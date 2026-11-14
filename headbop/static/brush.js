/* Bounded, device-independent brush editing. Server-side segmentation runs on Apply. */
(() => {
  "use strict";
  const clamp = (value, low = 0, high = 1) => Math.max(low, Math.min(high, value));
  class HeadbopBrush {
    constructor({ canvas, redraw, changed, limit }) {
      this.canvas = canvas;
      this.redraw = redraw;
      this.changed = changed;
      this.limit = limit;
      this.active = false;
      this.disabled = false;
      this.mode = "add";
      this.radius = 0.04;
      this.smart = true;
      this.strokes = [];
      this.undone = [];
      this.current = null;
      this.pointer = null;
      this.cursor = null;
      this.keyboardPainting = false;
      this.base = "empty";
      this.baseMask = null;
      this.overlay = document.createElement("canvas");
      this.frame = 0;
      canvas.addEventListener("pointerdown", (event) => this.pointerDown(event));
      canvas.addEventListener("pointermove", (event) => this.pointerMove(event));
      canvas.addEventListener("pointerup", (event) => this.pointerEnd(event));
      canvas.addEventListener("pointercancel", (event) => this.pointerEnd(event));
      canvas.addEventListener("lostpointercapture", () => this.finish());
      canvas.addEventListener("pointerleave", () => {
        if (this.pointer === null) { this.cursor = null; this.schedule(); }
      });
      canvas.addEventListener("keydown", (event) => this.keyDown(event));
      canvas.addEventListener("keyup", (event) => {
        if (event.code === "Space" && this.keyboardPainting) {
          event.preventDefault(); this.keyboardPainting = false; this.finish();
        }
      });
      canvas.addEventListener("blur", () => { this.keyboardPainting = false; this.finish(); });
    }
    open(baseMask = null) {
      this.finish(); this.active = true; this.baseMask = baseMask;
      this.base = baseMask ? "current" : "empty";
      this.strokes = []; this.undone = []; this.cursor = [0.5, 0.5];
      this.canvas.classList.add("painting"); this.changed(); this.schedule();
    }
    close() {
      this.finish(); this.active = false; this.cursor = null;
      this.canvas.classList.remove("painting"); this.schedule();
    }
    reset() { this.close(); this.baseMask = null; this.strokes = []; this.undone = []; this.base = "empty"; }
    setDisabled(value) { if (value) this.finish(); this.disabled = value; }
    setMode(mode) { this.finish(); this.mode = mode; this.changed(); this.schedule(); }
    clear() {
      if (this.disabled) return;
      this.finish(); this.base = "empty"; this.baseMask = null; this.strokes = []; this.undone = [];
      this.changed(); this.schedule();
    }
    undo() {
      if (this.disabled) return;
      this.finish(); if (this.strokes.length) this.undone.push(this.strokes.pop());
      this.changed(); this.schedule();
    }
    redo() {
      if (this.disabled) return;
      this.finish(); if (this.undone.length) this.strokes.push(this.undone.pop());
      this.changed(); this.schedule();
    }
    get pointCount() { return this.strokes.reduce((total, stroke) => total + stroke.points.length, 0) + (this.current?.points.length || 0); }
    get canApply() { return this.strokes.length > 0 || Boolean(this.current); }
    payload() {
      this.finish();
      return { base: this.base, smart: this.smart,
        strokes: this.strokes.map((stroke) => ({ mode: stroke.mode, radius: stroke.radius,
          points: stroke.points.map((point) => point.slice()) })) };
    }
    position(event) {
      const bounds = this.canvas.getBoundingClientRect();
      // Account for any object-fit letterbox instead of treating padding as image pixels.
      const scale = Math.min(bounds.width / this.canvas.width, bounds.height / this.canvas.height);
      const width = this.canvas.width * scale, height = this.canvas.height * scale;
      const left = bounds.left + (bounds.width - width) / 2;
      const top = bounds.top + (bounds.height - height) / 2;
      return [clamp((event.clientX - left) / width), clamp((event.clientY - top) / height)];
    }
    begin(point) {
      if (this.disabled || !this.active || this.current) return false;
      if (this.strokes.length >= 100 || this.pointCount >= 5000) { this.limit(); return false; }
      this.current = { mode: this.mode, radius: this.radius, points: [point.map((n) => Math.round(n * 10000) / 10000)] };
      this.undone = []; this.changed(); this.schedule(); return true;
    }
    append(point) {
      if (!this.current) return;
      const previous = this.current.points[this.current.points.length - 1];
      if (Math.hypot(point[0] - previous[0], point[1] - previous[1]) < 0.002) return;
      if (this.current.points.length >= 1000 || this.pointCount >= 5000) {
        this.finish(); this.limit(); return;
      }
      this.current.points.push(point.map((n) => Math.round(n * 10000) / 10000)); this.schedule();
    }
    finish() {
      if (this.current) { this.strokes.push(this.current); this.current = null; this.changed(); this.schedule(); }
      const pointer = this.pointer; this.pointer = null;
      if (pointer !== null && this.canvas.hasPointerCapture(pointer)) this.canvas.releasePointerCapture(pointer);
    }
    pointerDown(event) {
      if (!this.active || this.disabled || this.pointer !== null || (event.pointerType === "mouse" && event.button !== 0)) return;
      event.preventDefault(); this.canvas.focus({ preventScroll: true });
      this.cursor = this.position(event);
      if (this.begin(this.cursor)) { this.pointer = event.pointerId; this.canvas.setPointerCapture(event.pointerId); }
    }
    pointerMove(event) {
      if (!this.active || this.disabled) return;
      this.cursor = this.position(event);
      if (this.pointer === event.pointerId) { event.preventDefault(); this.append(this.cursor); }
      this.schedule();
    }
    pointerEnd(event) {
      if (this.pointer !== event.pointerId) return;
      if (!this.disabled) this.append(this.position(event));
      this.finish();
    }
    keyDown(event) {
      if (!this.active || this.disabled) return;
      const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      if (directions[event.key]) {
        event.preventDefault();
        const cursor = this.cursor || [0.5, 0.5], direction = directions[event.key];
        const step = event.shiftKey ? 0.05 : 0.01;
        this.cursor = [clamp(cursor[0] + direction[0] * step), clamp(cursor[1] + direction[1] * step)];
        if (this.keyboardPainting) this.append(this.cursor);
        this.schedule();
      } else if (event.code === "Space") {
        event.preventDefault();
        if (!this.keyboardPainting) { this.cursor ||= [0.5, 0.5]; this.keyboardPainting = this.begin(this.cursor); }
      } else if (event.key === "Escape") {
        event.preventDefault(); this.keyboardPainting = false; this.finish();
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault(); if (event.shiftKey) this.redo(); else this.undo();
      }
    }
    schedule() {
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => { this.frame = 0; this.redraw(); });
    }
    draw(context) {
      if (!this.active) return;
      const width = this.canvas.width, height = this.canvas.height;
      this.overlay.width = width; this.overlay.height = height;
      const mask = this.overlay.getContext("2d");
      if (this.baseMask && this.base === "current") {
        mask.drawImage(this.baseMask, 0, 0, width, height);
        mask.globalCompositeOperation = "source-in";
        mask.fillStyle = "#bdfb72"; mask.fillRect(0, 0, width, height);
      }
      for (const stroke of [...this.strokes, ...(this.current ? [this.current] : [])]) {
        const radius = stroke.radius * Math.min(width, height);
        mask.globalCompositeOperation = stroke.mode === "erase" ? "destination-out" : "source-over";
        mask.fillStyle = mask.strokeStyle = "#bdfb72";
        mask.lineCap = mask.lineJoin = "round"; mask.lineWidth = radius * 2;
        const first = stroke.points[0];
        mask.beginPath(); mask.arc(first[0] * width, first[1] * height, radius, 0, Math.PI * 2); mask.fill();
        mask.beginPath(); mask.moveTo(first[0] * width, first[1] * height);
        for (const point of stroke.points.slice(1)) mask.lineTo(point[0] * width, point[1] * height);
        mask.stroke();
      }
      context.save(); context.globalAlpha = 0.5; context.drawImage(this.overlay, 0, 0); context.restore();
      if (this.cursor && !this.disabled) {
        const x = this.cursor[0] * width, y = this.cursor[1] * height;
        const radius = this.radius * Math.min(width, height);
        context.save(); context.strokeStyle = "#111"; context.lineWidth = 4;
        context.beginPath(); context.arc(x, y, radius, 0, Math.PI * 2); context.stroke();
        context.strokeStyle = this.mode === "erase" ? "#f2b08f" : "#f4ffeb"; context.lineWidth = 2; context.stroke(); context.restore();
      }
    }
  }
  window.HeadbopBrush = HeadbopBrush;
})();
