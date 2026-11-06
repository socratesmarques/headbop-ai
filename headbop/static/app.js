/* Headbop's preview plays the exact MP4 served by the download endpoint. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = { project: null, busy: false, mode: "whole", motion: "smooth", selecting: false, rect: null, drag: null, image: null, dirty: false, files: { image: null, audio: null } };
const imageCanvas = $("image-canvas");
const imageContext = imageCanvas.getContext("2d");
let renderToken = 0;

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
  $("animation-controls").disabled = state.busy || !hasProject;
  $("analyze-button").disabled = state.busy || !state.files.image || !state.files.audio;
  $("render-button").disabled = state.busy || !hasProject || state.selecting || (state.mode === "subject" && !state.project.segmentation.available);
  $("clear-project").disabled = state.busy || !hasProject;
  $("import-button").disabled = state.busy;
  $("image-file").disabled = state.busy;
  $("audio-file").disabled = state.busy;
  $("select-region").disabled = state.busy;
  $("cancel-selection").disabled = state.busy;
  $("apply-selection").disabled = state.busy || !state.rect || state.rect[2] < 0.02 || state.rect[3] < 0.02;
  setLinkEnabled($("download-project"), hasProject && !state.busy);
  if (state.busy) setLinkEnabled($("download-video"), false);
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
  });
  $("segmentation-info").hidden = !hasProject || state.mode !== "subject";
  if (hasProject) {
    const segmentation = state.project.segmentation;
    $("segmentation-message").textContent = segmentation.warning || (segmentation.available ? "Recorte disponível. Você pode ajustar a seleção na imagem." : "Nenhuma cabeça detectada. Selecione a região do personagem na imagem.");
  }
}

function setBusy(value, label = "Preparando seu movimento…") {
  state.busy = value;
  $("render-overlay").hidden = !value;
  $("render-label").textContent = label;
  refreshControls();
}

function previewStatus(text, kind = "") {
  $("preview-state").textContent = text;
  $("preview-state").className = `preview-state ${kind}`;
}

function hideVideo() {
  const video = $("preview-video");
  video.pause();
  video.hidden = true;
  video.removeAttribute("src");
  video.load();
  setLinkEnabled($("download-video"), false);
  $("download-video").href = "#";
  imageCanvas.hidden = !state.project;
}

function dirty() {
  if (!state.project) return;
  state.dirty = true;
  hideVideo();
  previewStatus("AJUSTES PENDENTES", "dirty");
  $("preview-caption").textContent = "Imagem de origem — gere o preview para ver o movimento";
  $("render-hint").textContent = "Gere o preview para aplicar os ajustes e ouvir a música.";
  $("render-button").innerHTML = '<span aria-hidden="true">▶︎</span> Gerar preview';
  refreshControls();
}

function settings() {
  const bpm = Number($("bpm").value);
  if (!Number.isFinite(bpm) || bpm < 40 || bpm > 240) throw new Error("Informe um BPM entre 40 e 240.");
  return { mode: state.mode, motion: state.motion, bpm, intensity: Number($("intensity").value) };
}

function durationText(seconds) {
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function drawBeats() {
  const canvas = $("beat-canvas");
  const width = Math.max(1, canvas.clientWidth);
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = width * dpr;
  canvas.height = 50 * dpr;
  const context = canvas.getContext("2d");
  context.scale(dpr, dpr);
  context.strokeStyle = "#35402a";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(0, 45);
  context.lineTo(width, 45);
  context.stroke();
  if (!state.project) return;
  const { beats, intensity } = state.project.analysis;
  const duration = state.project.duration;
  beats.forEach((time, index) => {
    const x = time / duration * (width - 4);
    const height = 9 + Math.max(0, Math.min(1, intensity[index] || 0)) * 32;
    context.fillStyle = index % 4 === 0 ? "#d0f36a" : "#748f42";
    context.fillRect(x, 45 - height, 3, height);
  });
}

function drawImage() {
  if (!state.image) return;
  const width = imageCanvas.width;
  const height = imageCanvas.height;
  imageContext.clearRect(0, 0, width, height);
  imageContext.drawImage(state.image, 0, 0, width, height);
  if (state.selecting && state.rect) {
    const [x, y, w, h] = state.rect;
    imageContext.fillStyle = "rgba(10, 15, 5, .53)";
    imageContext.beginPath();
    imageContext.rect(0, 0, width, height);
    imageContext.rect(x * width, y * height, w * width, h * height);
    imageContext.fill("evenodd");
    imageContext.strokeStyle = "#d0f36a";
    imageContext.lineWidth = Math.max(2, width / 350);
    imageContext.strokeRect(x * width, y * height, w * width, h * height);
  }
}

async function loadProject(project) {
  state.project = project;
  state.mode = project.settings.mode;
  state.motion = project.settings.motion;
  state.rect = null;
  state.selecting = false;
  $("selection-toolbar").hidden = true;
  imageCanvas.classList.remove("selecting");
  $("bpm").value = Number(project.settings.bpm).toFixed(1).replace(/\.0$/, "");
  $("intensity").value = project.settings.intensity;
  $("intensity-value").value = `${Math.round(project.settings.intensity * 100)}%`;
  $("detected-bpm").textContent = Math.round(project.analysis.bpm);
  $("beat-count").textContent = project.analysis.beats.length;
  $("duration-label").textContent = durationText(project.duration);
  $("beat-summary").textContent = `${project.duration.toFixed(1)} s de música · ${project.keyframes.length} keyframes`;
  $("stage-empty").hidden = true;
  $("download-project").href = `/api/projects/${encodeURIComponent(project.id)}/export`;
  dirty();
  drawBeats();
  const image = new Image();
  image.onload = () => {
    if (state.project?.id !== project.id) return;
    state.image = image;
    const scale = Math.min(1, 1200 / Math.max(image.naturalWidth, image.naturalHeight));
    imageCanvas.width = Math.round(image.naturalWidth * scale);
    imageCanvas.height = Math.round(image.naturalHeight * scale);
    drawImage();
  };
  image.onerror = () => notice("Não foi possível carregar a imagem. Tente abrir o projeto novamente.", true);
  image.src = project.image_url;
  refreshControls();
}

function chooseFile(kind, file) {
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) {
    notice("Cada arquivo pode ter até 20 MB. Escolha um arquivo menor.", true);
    return;
  }
  state.files[kind] = file;
  $(`${kind}-name`).textContent = file.name;
  $(`${kind}-drop`).classList.add("selected");
  notice("");
  refreshControls();
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
  const body = new FormData();
  body.append("image", state.files.image);
  body.append("audio", state.files.audio);
  notice("");
  setBusy(true, "Encontrando o ritmo da sua música…");
  try {
    const project = await request("/api/projects", { method: "POST", body });
    await loadProject(project);
    notice("Análise concluída. Ajuste o movimento e gere seu preview.");
  } catch (error) { handleError(error); }
  finally { setBusy(false); }
});

document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
  state.mode = button.dataset.mode;
  if (state.mode === "whole") cancelSelection();
  dirty();
}));
document.querySelectorAll("[data-motion]").forEach((button) => button.addEventListener("click", () => { state.motion = button.dataset.motion; dirty(); }));
$("bpm").addEventListener("input", dirty);
$("bpm-down").addEventListener("click", () => { $("bpm").value = Math.max(40, Number($("bpm").value) - 1); dirty(); });
$("bpm-up").addEventListener("click", () => { $("bpm").value = Math.min(240, Number($("bpm").value) + 1); dirty(); });
$("intensity").addEventListener("input", () => { $("intensity-value").value = `${Math.round(Number($("intensity").value) * 100)}%`; dirty(); });

async function saveSettings() {
  const project = await request(`/api/projects/${state.project.id}/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings()) });
  state.project = project;
  $("beat-summary").textContent = `${project.duration.toFixed(1)} s de música · ${project.keyframes.length} keyframes`;
  return project;
}

$("render-button").addEventListener("click", async () => {
  if (!state.project || state.busy) return;
  try { settings(); } catch (error) { handleError(error); return; }
  const token = ++renderToken;
  notice("");
  setBusy(true, "Renderizando cada batida…");
  previewStatus("RENDERIZANDO");
  let videoURL = null;
  try {
    await saveSettings();
    const endpoint = `/api/projects/${state.project.id}/render`;
    await request(endpoint, { method: "POST" });
    const deadline = Date.now() + 300000;
    while (token === renderToken) {
      const result = await request(endpoint);
      if (result.status === "error") throw new Error(result.error || "A renderização falhou. Tente novamente.");
      if (result.status === "ready") {
        videoURL = `${result.video_url || `/api/projects/${state.project.id}/video`}?v=${Date.now()}`;
        const video = $("preview-video");
        video.src = videoURL;
        video.hidden = false;
        imageCanvas.hidden = true;
        video.load();
        state.dirty = false;
        previewStatus("PRONTO PARA DAR PLAY", "ready");
        $("preview-caption").textContent = "Este é o mesmo MP4 que você vai baixar";
        $("render-hint").textContent = "Dê play no vídeo ou baixe sua criação.";
        $("render-button").innerHTML = '<span aria-hidden="true">↻</span> Gerar novamente';
        $("download-video").href = `${videoURL}&download=true`;
        notice("Seu vídeo está pronto. Use o play da prévia para ouvir e assistir.");
        break;
      }
      if (Date.now() > deadline) throw new Error("A renderização está demorando mais que o esperado. Tente novamente para consultar ou gerar o vídeo.");
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  } catch (error) { handleError(error); previewStatus("REVISE E TENTE NOVAMENTE", "dirty"); }
  finally { setBusy(false); if (videoURL) setLinkEnabled($("download-video"), true); }
});

function cancelSelection() {
  state.selecting = false;
  state.rect = null;
  state.drag = null;
  imageCanvas.classList.remove("selecting");
  $("selection-toolbar").hidden = true;
  drawImage();
  refreshControls();
}

$("select-region").addEventListener("click", () => {
  if (!state.image) return;
  hideVideo();
  state.selecting = true;
  state.rect = null;
  imageCanvas.classList.add("selecting");
  $("selection-toolbar").hidden = false;
  previewStatus("SELECIONE UMA ÁREA", "dirty");
  drawImage();
  refreshControls();
  $("stage").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
});
$("cancel-selection").addEventListener("click", () => { cancelSelection(); dirty(); });

function pointerPosition(event) {
  const bounds = imageCanvas.getBoundingClientRect();
  return [Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)), Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height))];
}
imageCanvas.addEventListener("pointerdown", (event) => {
  if (!state.selecting || state.busy) return;
  event.preventDefault();
  state.drag = pointerPosition(event);
  state.rect = [state.drag[0], state.drag[1], 0, 0];
  imageCanvas.setPointerCapture(event.pointerId);
  drawImage();
});
imageCanvas.addEventListener("pointermove", (event) => {
  if (!state.drag || !state.selecting || state.busy) return;
  event.preventDefault();
  const point = pointerPosition(event);
  state.rect = [Math.min(state.drag[0], point[0]), Math.min(state.drag[1], point[1]), Math.abs(point[0] - state.drag[0]), Math.abs(point[1] - state.drag[1])];
  drawImage();
  refreshControls();
});
function endPointer(event) {
  if (imageCanvas.hasPointerCapture(event.pointerId)) imageCanvas.releasePointerCapture(event.pointerId);
  state.drag = null;
  refreshControls();
}
imageCanvas.addEventListener("pointerup", endPointer);
imageCanvas.addEventListener("pointercancel", endPointer);

$("apply-selection").addEventListener("click", async () => {
  if (!state.rect || !state.project || state.busy) return;
  const rect = state.rect.slice();
  setBusy(true, "Separando o personagem do fundo…");
  notice("");
  try {
    const project = await request(`/api/projects/${state.project.id}/segment`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rect }) });
    state.project = project;
    cancelSelection();
    dirty();
    notice(project.segmentation.available ? "Recorte aplicado. Gere o preview para conferir o resultado." : (project.segmentation.warning || "Não foi possível recortar. Tente uma seleção com mais contraste."), !project.segmentation.available);
  } catch (error) { handleError(error); }
  finally { setBusy(false); }
});

$("import-button").addEventListener("click", () => $("import-file").click());
$("import-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file || state.busy) return;
  event.target.value = "";
  if (file.size > 56 * 1024 * 1024) { notice("O projeto XML deve ter até 56 MiB.", true); return; }
  const body = new FormData();
  body.append("file", file);
  setBusy(true, "Abrindo seu projeto…");
  notice("");
  try {
    await loadProject(await request("/api/import", { method: "POST", body }));
    notice("Projeto Headbop importado. Gere o preview com os ajustes salvos.");
  } catch (error) { handleError(error); }
  finally { setBusy(false); }
});

$("download-project").addEventListener("click", async (event) => {
  event.preventDefault();
  if (!state.project || state.busy) return;
  try { settings(); } catch (error) { handleError(error); return; }
  const previousVideoReady = !$("download-video").classList.contains("disabled");
  setBusy(true, "Salvando imagem, áudio e movimento…");
  notice("");
  try {
    if (state.dirty) await saveSettings();
    const response = await fetch(`/api/projects/${state.project.id}/export`);
    if (!response.ok) throw new Error("Não foi possível exportar o projeto. Tente novamente.");
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "headbop-project.xml";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    notice("Projeto exportado no formato Headbop XML. Use “Abrir projeto” para continuar depois.");
  } catch (error) { handleError(error); }
  finally { setBusy(false); if (previousVideoReady) setLinkEnabled($("download-video"), true); }
});
$("download-video").addEventListener("click", (event) => { if (event.currentTarget.classList.contains("disabled")) event.preventDefault(); });

$("clear-project").addEventListener("click", async () => {
  if (!state.project || state.busy) return;
  setBusy(true, "Removendo arquivos desta sessão…");
  notice("");
  try {
    await request(`/api/projects/${state.project.id}`, { method: "DELETE" });
    renderToken += 1;
    state.project = null;
    state.image = null;
    state.files = { image: null, audio: null };
    state.dirty = false;
    state.mode = "whole";
    state.motion = "smooth";
    hideVideo();
    cancelSelection();
    $("upload-form").reset();
    $("image-name").textContent = "Escolha uma imagem";
    $("audio-name").textContent = "Escolha sua música";
    $("image-drop").classList.remove("selected");
    $("audio-drop").classList.remove("selected");
    $("stage-empty").hidden = false;
    $("detected-bpm").textContent = "—";
    $("beat-count").textContent = "—";
    $("duration-label").textContent = "—";
    $("beat-summary").textContent = "A sua música define o movimento.";
    $("render-hint").textContent = "Adicione seus arquivos para gerar a animação.";
    $("preview-caption").textContent = "Preview fiel ao MP4 exportado";
    previewStatus("AGUARDANDO ARQUIVOS");
    drawBeats();
    notice("Arquivos do projeto removidos deste servidor.");
  } catch (error) { handleError(error); }
  finally { setBusy(false); }
});

window.addEventListener("resize", drawBeats);
$("preview-video").addEventListener("error", () => {
  if (!$("preview-video").hidden && $("preview-video").getAttribute("src")) notice("O navegador não conseguiu reproduzir o vídeo. Baixe o MP4 para abri-lo em um player compatível.", true);
});
refreshControls();
drawBeats();
