/* Headbop's preview plays the exact MP4 served by the download endpoint. */
"use strict";

const $ = (id) => document.getElementById(id);
const defaults = { fps: 24, resolution: 640, aspect: "source", speed: 1, easing: "natural", squash: 0.25 };
const state = { project: null, busy: false, mode: "whole", motion: "auto", engine: "v2", selecting: false, selectionKind: "subject", rect: null, drag: null, image: null, mask: null, view: "source", videoURL: null, dirty: false, files: { image: null, audio: null } };
const imageCanvas = $("image-canvas");
const imageContext = imageCanvas.getContext("2d");
let renderToken = 0;
let imageToken = 0;
let playbackFrame = 0;

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Não foi possível concluir a operação (${response.status}).`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((item) => item.msg).join("; ");
    } catch (_) { /* A proxy may return a non-JSON error. */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function handleError(error) {
  notice(error instanceof TypeError ? "A conexão com o servidor falhou. Verifique se ele está ativo e tente novamente." : error.message, true);
}

function setLinkEnabled(element, enabled) {
  element.classList.toggle("disabled", !enabled);
  element.setAttribute("aria-disabled", String(!enabled));
  element.tabIndex = enabled ? 0 : -1;
}

function refreshControls() {
  const hasProject = Boolean(state.project);
  const legacy = state.engine === "legacy";
  $("animation-controls").disabled = state.busy || !hasProject;
  $("v2-controls").disabled = legacy;
  $("analyze-button").disabled = state.busy || !state.files.image || !state.files.audio;
  $("render-button").disabled = state.busy || !hasProject || state.selecting || (state.mode === "subject" && !state.project.segmentation.available);
  $("clear-project").disabled = state.busy || !hasProject;
  $("import-button").disabled = state.busy;
  $("image-file").disabled = state.busy;
  $("audio-file").disabled = state.busy;
  $("select-region").disabled = state.busy || !state.image;
  $("select-head").disabled = state.busy || !state.image || legacy;
  $("cancel-selection").disabled = state.busy;
  $("apply-selection").disabled = state.busy || !state.rect || state.rect[2] < 0.02 || state.rect[3] < 0.02;
  $("preview-tools").hidden = !hasProject;
  $("timeline-seek").disabled = state.busy || !state.videoURL || state.selecting;
  $("engine-note").textContent = legacy ? "Motor original: mantém o movimento e a saída dos projetos antigos. Selecione Headbop 2.0 para usar os novos ajustes." : "O modo Auto acompanha a energia e os ataques da música.";
  $("output-label").textContent = `${$("fps").value} FPS`;
  setLinkEnabled($("download-project"), hasProject && !state.busy);
  setLinkEnabled($("download-video"), Boolean(state.videoURL) && !state.busy);
  document.body.classList.toggle("busy", state.busy);
  document.querySelectorAll("[data-mode]").forEach((button) => {
    const active = button.dataset.mode === state.mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  document.querySelectorAll("[data-motion]").forEach((button) => {
    const active = button.dataset.motion === state.motion;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = legacy && button.dataset.motion === "auto";
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const view = button.dataset.view;
    button.setAttribute("aria-pressed", String(view === state.view));
    button.disabled = state.busy || state.selecting || (view === "video" && !state.videoURL) || (view === "mask" && !state.mask);
  });
  $("segmentation-info").hidden = !hasProject || state.mode !== "subject";
  if (hasProject) {
    const segmentation = state.project.segmentation;
    $("segmentation-message").textContent = segmentation.warning || (segmentation.available ? "Recorte disponível. Você pode ajustar a seleção na imagem." : "Recorte indisponível. Selecione a região do personagem na imagem.");
  }
}

function updateProgress(percent = null) {
  const track = $("render-progress-track");
  const fill = $("render-progress-fill");
  const detail = $("render-progress-detail");
  const known = Number.isFinite(percent);
  track.classList.toggle("indeterminate", !known);
  track.setAttribute("aria-valuetext", known ? `${Math.max(0, Math.min(100, Math.round(percent)))}%` : (window.headbopI18n?.t("Processando…") || "Processando…"));
  if (known) {
    const value = Math.max(0, Math.min(100, Math.round(percent)));
    track.setAttribute("aria-valuenow", String(value));
    fill.style.width = `${value}%`;
    detail.textContent = `${value}%`;
  } else {
    track.removeAttribute("aria-valuenow");
    fill.style.width = "";
    detail.textContent = window.headbopI18n?.t("Processando…") || "Processando…";
  }
}

function setBusy(value, label = "Preparando seu movimento…") {
  state.busy = value;
  $("render-overlay").hidden = !value;
  $("render-label").textContent = window.headbopI18n?.t(label) || label;
  if (value) updateProgress();
  refreshControls();
}

function previewStatus(text, kind = "") {
  $("preview-state").textContent = text;
  $("preview-state").className = `preview-state ${kind}`;
}

function showView(view) {
  if (view === "video" && !state.videoURL) return;
  state.view = view;
  const video = $("preview-video");
  if (view !== "video") video.pause();
  video.hidden = view !== "video";
  imageCanvas.hidden = !state.project || view === "video";
  const note = $("inspection-note");
  note.hidden = !["mask", "rig"].includes(view);
  if (view === "mask") note.textContent = "Les zones transparentes restent fixes dans le fond."; // Replaced below with the studio language.
  if (view === "mask") note.textContent = "O quadriculado mostra as áreas transparentes do recorte. Ajuste a seleção se houver partes faltando.";
  if (view === "rig") {
    const rig = state.project.rig;
    note.textContent = rig?.warning || (rig?.kind === "head-body" ? "Pivôs de corpo e cabeça. Marque a cabeça para ajustar a divisão das camadas." : "Movimento em uma camada. Marque a cabeça para tentar separar cabeça e corpo.");
  }
  if (!state.selecting) $("preview-caption").textContent = view === "video" ? "Este é o mesmo MP4 que você vai baixar" : { source: "Imagem de origem", mask: "Máscara do personagem", rig: "Pivôs de rotação · corpo em verde, cabeça em âmbar" }[view];
  drawImage();
  refreshControls();
}

function invalidatePreview() {
  const video = $("preview-video");
  video.pause();
  video.hidden = true;
  video.removeAttribute("src");
  video.load();
  state.videoURL = null;
  $("download-video").href = "#";
  $("timeline-seek").value = 0;
  showView("source");
  previewStatus("AJUSTES PENDENTES", "dirty");
  $("preview-caption").textContent = "Imagem de origem — gere o preview para ver o movimento";
  $("render-hint").textContent = "Gere o preview para aplicar os ajustes e ouvir a música.";
  $("render-button").innerHTML = '<span aria-hidden="true">▶︎</span> Gerar preview';
  updatePlayhead();
}

function dirty() {
  if (!state.project) return;
  state.dirty = true;
  invalidatePreview();
}

function settings() {
  const bpm = Number($("bpm").value);
  if (!Number.isFinite(bpm) || bpm < 40 || bpm > 240) throw new Error("Informe um BPM entre 40 e 240.");
  const output = state.engine === "legacy" ? defaults : { fps: Number($("fps").value), resolution: Number($("resolution").value), aspect: $("aspect").value, speed: Number($("speed").value), easing: $("easing").value, squash: Number($("squash").value) };
  return { mode: state.mode, motion: state.motion, engine: state.engine, bpm, intensity: Number($("intensity").value), ...output };
}

function durationText(seconds, precise = false) {
  const value = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(Math.floor(value % 60)).padStart(2, "0")}${precise ? `.${Math.floor(value * 10) % 10}` : ""}`;
}

function drawBeats() {
  const canvas = $("beat-canvas");
  const width = Math.max(1, canvas.clientWidth);
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = width * dpr;
  canvas.height = 64 * dpr;
  const context = canvas.getContext("2d");
  context.scale(dpr, dpr);
  context.strokeStyle = "#35402a";
  context.beginPath(); context.moveTo(0, 58); context.lineTo(width, 58); context.stroke();
  if (!state.project) return;
  const { beats = [], intensity = [], energy = [], onsets = [], silences = [] } = state.project.analysis;
  const duration = state.project.duration;
  const position = (time) => Math.max(1, Math.min(width - 1, time / duration * width));
  silences.forEach(([start, end]) => { context.fillStyle = "#080b0666"; context.fillRect(position(start), 0, position(end) - position(start), 58); });
  if (energy.length) {
    context.beginPath(); context.moveTo(0, 58);
    energy.forEach((sample) => context.lineTo(position(sample.time), 56 - Math.max(0, Math.min(1, sample.value)) * 48));
    context.lineTo(width, 58); context.closePath(); context.fillStyle = "#748f424d"; context.fill();
    context.beginPath();
    energy.forEach((sample, index) => { const x = position(sample.time); const y = 56 - Math.max(0, Math.min(1, sample.value)) * 48; if (index) context.lineTo(x, y); else context.moveTo(x, y); });
    context.strokeStyle = "#748f42"; context.stroke();
  }
  beats.forEach((time, index) => {
    const height = 9 + Math.max(0, Math.min(1, intensity[index] || 0)) * 32;
    context.fillStyle = index % 4 === 0 ? "#d0f36a" : "#8da957";
    context.fillRect(position(time), 58 - height, 2, height);
  });
  onsets.forEach((time) => {
    const x = position(time); context.fillStyle = "#eabc69";
    context.beginPath(); context.moveTo(x, 2); context.lineTo(x - 3, 7); context.lineTo(x + 3, 7); context.closePath(); context.fill();
  });
  if (state.videoURL) {
    const x = position($("preview-video").currentTime || 0);
    context.strokeStyle = "#f2f4e9"; context.lineWidth = 1.5;
    context.beginPath(); context.moveTo(x, 0); context.lineTo(x, 62); context.stroke();
  }
}

function updatePlayhead() {
  const current = state.videoURL ? $("preview-video").currentTime || 0 : 0;
  $("timeline-seek").value = current;
  $("playhead-label").value = durationText(current, true);
  $("timeline-seek").setAttribute("aria-valuetext", `${durationText(current, true)} de ${durationText(state.project?.duration || 0, true)}`);
  drawBeats();
}

function animatePlayhead() {
  updatePlayhead();
  if (!$("preview-video").paused) playbackFrame = requestAnimationFrame(animatePlayhead);
}

function drawImage() {
  if (!state.image) return;
  const width = imageCanvas.width;
  const height = imageCanvas.height;
  imageContext.clearRect(0, 0, width, height);
  if (state.view === "mask" && state.mask && !state.selecting) {
    const tile = Math.max(12, Math.round(width / 24));
    for (let y = 0; y < height; y += tile) for (let x = 0; x < width; x += tile) {
      imageContext.fillStyle = (Math.floor(x / tile) + Math.floor(y / tile)) % 2 ? "#30372b" : "#1f261a";
      imageContext.fillRect(x, y, tile, tile);
    }
    imageContext.drawImage(state.mask, 0, 0, width, height);
  } else imageContext.drawImage(state.image, 0, 0, width, height);
  if (state.view === "rig" && !state.selecting) {
    const rig = state.project.rig;
    if (rig?.head_rect) {
      const [x, y, w, h] = rig.head_rect;
      imageContext.strokeStyle = "#eabc69"; imageContext.lineWidth = Math.max(2, width / 300);
      imageContext.setLineDash([width / 50, width / 75]);
      imageContext.strokeRect(x * width, y * height, w * width, h * height); imageContext.setLineDash([]);
    }
    const pivots = rig?.pivots || {};
    if (pivots.body && pivots.head) {
      imageContext.strokeStyle = "#f2f4e999"; imageContext.lineWidth = Math.max(1, width / 400);
      imageContext.beginPath(); imageContext.moveTo(pivots.body[0] * width, pivots.body[1] * height); imageContext.lineTo(pivots.head[0] * width, pivots.head[1] * height); imageContext.stroke();
    }
    Object.entries(pivots).forEach(([name, point]) => {
      if (!Array.isArray(point)) return;
      const x = point[0] * width; const y = point[1] * height; const radius = Math.max(6, width / 65);
      imageContext.strokeStyle = "#10110e"; imageContext.lineWidth = Math.max(4, width / 130);
      imageContext.beginPath(); imageContext.arc(x, y, radius, 0, Math.PI * 2); imageContext.stroke();
      imageContext.strokeStyle = name === "head" ? "#eabc69" : "#d0f36a"; imageContext.lineWidth = Math.max(2, width / 260);
      imageContext.stroke(); imageContext.beginPath(); imageContext.moveTo(x - radius * 1.5, y); imageContext.lineTo(x + radius * 1.5, y); imageContext.moveTo(x, y - radius * 1.5); imageContext.lineTo(x, y + radius * 1.5); imageContext.stroke();
    });
  }
  if (state.selecting && state.rect) {
    const [x, y, w, h] = state.rect;
    imageContext.fillStyle = "rgba(10, 15, 5, .53)";
    imageContext.beginPath(); imageContext.rect(0, 0, width, height); imageContext.rect(x * width, y * height, w * width, h * height); imageContext.fill("evenodd");
    imageContext.strokeStyle = state.selectionKind === "head" ? "#eabc69" : "#d0f36a";
    imageContext.lineWidth = Math.max(2, width / 350);
    imageContext.strokeRect(x * width, y * height, w * width, h * height);
  }
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = url;
  });
}

async function loadMask(project) {
  state.mask = null;
  const token = ++imageToken;
  const url = project.mask_url || project.foreground_url;
  if (url && project.segmentation.available) {
    try {
      const mask = await loadImage(`${url}${url.includes("?") ? "&" : "?"}v=${Date.now()}`);
      if (token === imageToken && state.project?.id === project.id) state.mask = mask;
    } catch (_) { /* Source and manual selection remain usable if the mask cannot load. */ }
  }
  if (token === imageToken) { drawImage(); refreshControls(); }
}

function updateSummary() {
  const project = state.project;
  $("beat-summary").textContent = `${project.duration.toFixed(1)} s de música · ${project.keyframes.length} keyframes`;
}

function rangeLabels() {
  $("intensity-value").value = `${Math.round(Number($("intensity").value) * 100)}%`;
  $("speed-value").value = `${Number($("speed").value)}×`;
  $("squash-value").value = `${Math.round(Number($("squash").value) * 100)}%`;
}

async function loadProject(project) {
  state.project = project;
  state.mode = project.settings.mode;
  state.motion = project.settings.motion;
  state.engine = project.settings.engine || "legacy";
  state.image = null;
  state.mask = null;
  state.dirty = false;
  cancelSelection();
  $("engine").value = state.engine;
  $("bpm").value = Number(project.settings.bpm).toFixed(1).replace(/\.0$/, "");
  $("intensity").value = project.settings.intensity;
  for (const [name, fallback] of Object.entries(defaults)) $(name).value = project.settings[name] ?? fallback;
  rangeLabels();
  $("detected-bpm").textContent = Math.round(project.analysis.bpm);
  $("beat-count").textContent = project.analysis.beats.length;
  $("duration-label").textContent = durationText(project.duration, true);
  $("timeline-seek").max = project.duration;
  updateSummary();
  $("stage-empty").hidden = true;
  $("download-project").href = `/api/projects/${encodeURIComponent(project.id)}/export`;
  invalidatePreview();
  try {
    const image = await loadImage(project.image_url);
    if (state.project?.id !== project.id) return;
    state.image = image;
    const scale = Math.min(1, 1200 / Math.max(image.naturalWidth, image.naturalHeight));
    imageCanvas.width = Math.round(image.naturalWidth * scale);
    imageCanvas.height = Math.round(image.naturalHeight * scale);
    drawImage();
  } catch (_) { notice("Não foi possível carregar a imagem. Tente abrir o projeto novamente.", true); }
  await loadMask(project);
  refreshControls();
}

function chooseFile(kind, file) {
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) { notice("Cada arquivo pode ter até 20 MB. Escolha um arquivo menor.", true); return; }
  state.files[kind] = file;
  $(`${kind}-name`).textContent = file.name;
  $(`${kind}-drop`).classList.add("selected");
  notice(""); refreshControls();
}

for (const kind of ["image", "audio"]) {
  $(`${kind}-file`).addEventListener("change", (event) => chooseFile(kind, event.target.files[0]));
  const drop = $(`${kind}-drop`);
  drop.addEventListener("dragover", (event) => { event.preventDefault(); if (!state.busy) drop.classList.add("dragging"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
  drop.addEventListener("drop", (event) => { event.preventDefault(); drop.classList.remove("dragging"); if (!state.busy) chooseFile(kind, event.dataTransfer.files[0]); });
}

$("upload-form").noValidate = true; // Dropped File objects are stored without mutating native file inputs.
$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy || !state.files.image || !state.files.audio) return;
  const body = new FormData(); body.append("image", state.files.image); body.append("audio", state.files.audio);
  notice(""); setBusy(true, "Encontrando o ritmo da sua música…");
  let created = false;
  try { await loadProject(await request("/api/projects", { method: "POST", body })); created = true; }
  catch (error) { handleError(error); }
  finally { setBusy(false); }
  if (created) await renderProject();
});

document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
  state.mode = button.dataset.mode;
  cancelSelection(); dirty();
}));
document.querySelectorAll("[data-motion]").forEach((button) => button.addEventListener("click", () => { state.motion = button.dataset.motion; dirty(); }));
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$("engine").addEventListener("change", () => {
  state.engine = $("engine").value;
  if (state.engine === "legacy") {
    if (state.motion === "auto") state.motion = "smooth";
    for (const [name, value] of Object.entries(defaults)) $(name).value = value;
    rangeLabels();
  }
  cancelSelection(); dirty();
});
$("bpm").addEventListener("input", dirty);
$("bpm-down").addEventListener("click", () => { $("bpm").value = Math.max(40, Number($("bpm").value) - 1); dirty(); });
$("bpm-up").addEventListener("click", () => { $("bpm").value = Math.min(240, Number($("bpm").value) + 1); dirty(); });
for (const name of ["intensity", "speed", "squash"]) $(name).addEventListener("input", () => { rangeLabels(); dirty(); });
for (const name of ["fps", "resolution", "aspect", "easing"]) $(name).addEventListener("change", dirty);

async function saveSettings() {
  const project = await request(`/api/projects/${state.project.id}/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings()) });
  state.project = project;
  state.dirty = false;
  updateSummary();
  return project;
}

async function renderProject() {
  if (!state.project || state.busy || state.selecting) return;
  try { settings(); } catch (error) { handleError(error); return; }
  const token = ++renderToken;
  notice(""); setBusy(true, "Renderizando cada batida…"); previewStatus("RENDERIZANDO");
  try {
    if (state.dirty) await saveSettings();
    const endpoint = `/api/projects/${state.project.id}/render`;
    await request(endpoint, { method: "POST" });
    const deadline = Date.now() + 300000;
    while (token === renderToken) {
      const result = await request(endpoint);
      if (result.status === "queued") $("render-label").textContent = window.headbopI18n?.t("Na fila de renderização…") || "Na fila de renderização…";
      if (result.status === "rendering") $("render-label").textContent = window.headbopI18n?.t("Renderizando cada batida…") || "Renderizando cada batida…";
      if (Number.isFinite(result.progress)) updateProgress(result.progress);
      if (result.status === "error") throw new Error(result.error || "A renderização falhou. Tente novamente.");
      if (result.status === "ready") {
        state.videoURL = `${result.video_url || `/api/projects/${state.project.id}/video`}?v=${Date.now()}`;
        const video = $("preview-video"); video.src = state.videoURL; video.load();
        state.dirty = false; showView("video");
        previewStatus("PRONTO PARA DAR PLAY", "ready");
        $("render-hint").textContent = "Dê play no vídeo ou baixe sua criação.";
        $("render-button").innerHTML = '<span aria-hidden="true">↻</span> Gerar novamente';
        $("download-video").href = `${state.videoURL}&download=true`;
        $("timeline-hint").textContent = "Arraste a linha do tempo ou toque no mapa para explorar o vídeo.";
        notice("Seu vídeo está pronto. Use o play da prévia para ouvir e assistir.");
        updatePlayhead(); break;
      }
      if (Date.now() > deadline) throw new Error("A renderização está demorando mais que o esperado. Tente novamente para consultar ou gerar o vídeo.");
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  } catch (error) { handleError(error); previewStatus("REVISE E TENTE NOVAMENTE", "dirty"); }
  finally { setBusy(false); }
}
$("render-button").addEventListener("click", renderProject);

function cancelSelection() {
  state.selecting = false; state.rect = null; state.drag = null;
  imageCanvas.classList.remove("selecting"); $("selection-toolbar").hidden = true;
  drawImage(); refreshControls();
}

function startSelection(kind) {
  if (!state.image || state.busy) return;
  state.selectionKind = kind;
  showView("source"); state.selecting = true; state.rect = null;
  imageCanvas.classList.add("selecting"); $("selection-toolbar").hidden = false;
  $("selection-instruction").textContent = kind === "head" ? "Arraste um retângulo ao redor da cabeça. O pivô ficará na base, perto do pescoço." : "Arraste para envolver o personagem. Inclua uma pequena margem de fundo.";
  $("apply-selection").textContent = kind === "head" ? "Aplicar cabeça" : "Aplicar recorte";
  $("preview-caption").textContent = kind === "head" ? "Seleção manual da cabeça" : "Seleção manual do personagem";
  drawImage(); refreshControls();
  $("stage").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
}
$("select-region").addEventListener("click", () => startSelection("subject"));
$("select-head").addEventListener("click", () => startSelection("head"));
$("cancel-selection").addEventListener("click", () => { cancelSelection(); showView(state.videoURL ? "video" : "source"); });

function pointerPosition(event) {
  const bounds = imageCanvas.getBoundingClientRect();
  return [Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)), Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height))];
}
imageCanvas.addEventListener("pointerdown", (event) => {
  if (!state.selecting || state.busy) return;
  event.preventDefault(); state.drag = pointerPosition(event); state.rect = [state.drag[0], state.drag[1], 0, 0];
  imageCanvas.setPointerCapture(event.pointerId); drawImage();
});
imageCanvas.addEventListener("pointermove", (event) => {
  if (!state.drag || !state.selecting || state.busy) return;
  event.preventDefault(); const point = pointerPosition(event);
  state.rect = [Math.min(state.drag[0], point[0]), Math.min(state.drag[1], point[1]), Math.abs(point[0] - state.drag[0]), Math.abs(point[1] - state.drag[1])];
  drawImage(); refreshControls();
});
function endPointer(event) {
  if (imageCanvas.hasPointerCapture(event.pointerId)) imageCanvas.releasePointerCapture(event.pointerId);
  state.drag = null; refreshControls();
}
imageCanvas.addEventListener("pointerup", endPointer);
imageCanvas.addEventListener("pointercancel", endPointer);

$("apply-selection").addEventListener("click", async () => {
  if (!state.rect || !state.project || state.busy) return;
  const head = state.selectionKind === "head";
  const body = head ? { head_rect: state.rect.slice() } : { rect: state.rect.slice() };
  setBusy(true, head ? "Ajustando as camadas e os pivôs…" : "Separando o personagem do fundo…"); notice("");
  try {
    const project = await request(`/api/projects/${state.project.id}/${head ? "rig" : "segment"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    state.project = project; cancelSelection(); dirty(); updateSummary(); await loadMask(project);
    if (head) { showView("rig"); notice(project.rig?.warning || "Cabeça marcada. Gere o preview para conferir o movimento."); }
    else notice(project.segmentation.available ? "Recorte aplicado. Gere o preview para conferir o resultado." : (project.segmentation.warning || "Não foi possível recortar. Tente uma seleção com mais contraste."), !project.segmentation.available);
  } catch (error) { handleError(error); }
  finally { setBusy(false); }
});

function seekTo(time) {
  if (!state.videoURL || state.busy || state.selecting) return;
  const video = $("preview-video");
  if (video.readyState < 1) return;
  video.currentTime = Math.max(0, Math.min(state.project.duration, time));
  showView("video"); updatePlayhead();
}
$("timeline-seek").addEventListener("input", (event) => seekTo(Number(event.target.value)));
$("beat-canvas").addEventListener("click", (event) => {
  if (!state.project) return;
  const bounds = event.currentTarget.getBoundingClientRect(); seekTo((event.clientX - bounds.left) / bounds.width * state.project.duration);
});

$("import-button").addEventListener("click", () => $("import-file").click());
$("import-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file || state.busy) return;
  event.target.value = "";
  if (file.size > 56 * 1024 * 1024) { notice("O projeto XML deve ter até 56 MiB.", true); return; }
  const body = new FormData(); body.append("file", file);
  setBusy(true, "Abrindo seu projeto…"); notice("");
  try { await loadProject(await request("/api/import", { method: "POST", body })); notice("Projeto Headbop importado. Gere o preview com os ajustes salvos."); }
  catch (error) { handleError(error); }
  finally { setBusy(false); }
});

$("download-project").addEventListener("click", async (event) => {
  event.preventDefault();
  if (!state.project || state.busy) return;
  try { settings(); } catch (error) { handleError(error); return; }
  setBusy(true, "Salvando imagem, áudio e movimento…"); notice("");
  try {
    if (state.dirty) await saveSettings();
    const response = await fetch(`/api/projects/${state.project.id}/export`);
    if (!response.ok) throw new Error("Não foi possível exportar o projeto. Tente novamente.");
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = "headbop-project.xml";
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    notice("Projeto exportado no formato Headbop XML. Use “Abrir projeto” para continuar depois.");
  } catch (error) { handleError(error); }
  finally { setBusy(false); }
});
$("download-video").addEventListener("click", (event) => { if (event.currentTarget.classList.contains("disabled")) event.preventDefault(); });

$("clear-project").addEventListener("click", async () => {
  if (!state.project || state.busy) return;
  setBusy(true, "Removendo arquivos desta sessão…"); notice("");
  try {
    await request(`/api/projects/${state.project.id}`, { method: "DELETE" });
    renderToken += 1; imageToken += 1;
    state.project = null; state.image = null; state.mask = null; state.files = { image: null, audio: null }; state.dirty = false;
    state.mode = "whole"; state.motion = "auto"; state.engine = "v2";
    $("engine").value = "v2";
    for (const [name, value] of Object.entries(defaults)) $(name).value = value;
    $("bpm").value = 120; $("intensity").value = 1; rangeLabels();
    invalidatePreview(); cancelSelection(); $("upload-form").reset();
    $("image-name").textContent = "Escolha uma imagem"; $("audio-name").textContent = "Escolha sua música";
    $("image-drop").classList.remove("selected"); $("audio-drop").classList.remove("selected");
    $("stage-empty").hidden = false; $("detected-bpm").textContent = "—"; $("beat-count").textContent = "—"; $("duration-label").textContent = "—";
    $("beat-summary").textContent = "A sua música define o movimento."; $("render-hint").textContent = "Adicione seus arquivos para gerar a animação.";
    $("preview-caption").textContent = "Preview fiel ao MP4 exportado"; $("timeline-hint").textContent = "Depois do render, arraste a linha do tempo para explorar o vídeo.";
    previewStatus("AGUARDANDO ARQUIVOS"); drawBeats(); notice("Arquivos do projeto removidos deste servidor.");
  } catch (error) { handleError(error); }
  finally { setBusy(false); }
});

window.addEventListener("resize", drawBeats);
const previewVideo = $("preview-video");
for (const event of ["timeupdate", "seeked", "loadedmetadata", "pause", "ended"]) previewVideo.addEventListener(event, updatePlayhead);
previewVideo.addEventListener("play", () => { cancelAnimationFrame(playbackFrame); animatePlayhead(); });
previewVideo.addEventListener("error", () => {
  if (previewVideo.getAttribute("src")) notice("O navegador não conseguiu reproduzir o vídeo. Baixe o MP4 para abri-lo em um player compatível.", true);
});
refreshControls(); drawBeats();

/* Language persists between visits; change labels without resetting project state. */
const languagePicker = $("language-select");
if (window.headbopI18n && languagePicker) {
  languagePicker.value = window.headbopI18n.language;
  languagePicker.addEventListener("change", () => window.headbopI18n.setLanguage(languagePicker.value));
}
