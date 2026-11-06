"""HTTP application and bounded local processing pipeline."""

import io
import logging
import os
import shutil
import subprocess
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from .animation import generate_keyframes
from .audio import analyze_audio
from .formats import export_project, import_project
from .imaging import segment_image
from .renderer import render_video
from .store import Store

MAX_UPLOAD = 20 * 1024 * 1024
MAX_XML = 64 * 1024 * 1024
LOGGER = logging.getLogger("headbop")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["whole", "subject"] = "whole"
    motion: Literal["smooth", "hard", "velocity"] = "smooth"
    bpm: float = Field(ge=40, le=240)
    intensity: float = Field(default=1, ge=0, le=2)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    rect: list[float] = Field(min_length=4, max_length=4)


def read_upload(upload: UploadFile, limit: int) -> bytes:
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Arquivo excede o limite de {limit // 1024 // 1024} MiB.")
    if not data:
        raise ValueError("O arquivo está vazio.")
    return data


def normalize_image(data: bytes, target: Path) -> tuple[int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > 16_000_000:
                    raise ValueError("Use uma imagem de até 16 megapixels.")
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("Use uma imagem PNG, JPEG ou WebP.")
                result = ImageOps.exif_transpose(source).convert("RGBA")
                result.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                if min(result.size) < 32:
                    raise ValueError("A imagem deve ter pelo menos 32 pixels de cada lado.")
                result.save(target, format="PNG")
                return result.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("Imagem inválida, danificada ou grande demais.") from exc


def normalize_audio(data: bytes, target: Path):
    if not shutil.which("ffmpeg"):
        raise ValueError("FFmpeg não está instalado no servidor.")
    source = target.with_name("upload.audio")
    source.write_bytes(data)
    try:
        process = subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file,pipe", "-format_whitelist",
            "wav,mp3,flac,ogg,mov,matroska,aac", "-threads", "1", "-i", str(source),
            "-vn", "-t", "31", "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le",
            str(target),
        ], capture_output=True, timeout=45)
        if process.returncode:
            raise ValueError("Áudio inválido ou formato não suportado pelo FFmpeg.")
        info = sf.info(target)
        if info.duration > 30.001:
            raise ValueError("Use um trecho de áudio de até 30 segundos.")
    except subprocess.TimeoutExpired as exc:
        raise ValueError("O áudio demorou demais para decodificar.") from exc
    finally:
        source.unlink(missing_ok=True)


def frames_for(project: dict) -> list[dict]:
    settings = project["settings"]
    return generate_keyframes(project["duration"], project["analysis"]["beats"],
                              project["analysis"]["intensity"], settings["bpm"],
                              settings["intensity"], settings["motion"])


def public(project: dict) -> dict:
    return {key: value for key, value in project.items() if key != "render"}


def create_app(data_dir: Path | None = None) -> FastAPI:
    store = Store(data_dir or Path(os.getenv("HEADBOP_DATA_DIR", "/tmp/headbop-ai")))
    pool = None
    stop = threading.Event()

    def cleanup_loop():
        while not stop.wait(300):
            store.cleanup()

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal pool, stop
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="headbop-render")
        stop = threading.Event()
        store.cleanup()
        cleaner = threading.Thread(target=cleanup_loop, daemon=True)
        cleaner.start()
        yield
        stop.set()
        pool.shutdown(wait=True)
        cleaner.join(timeout=1)
        pool = None

    app = FastAPI(title="Headbop AI", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.store = store

    class BodyLimit:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            received = 0
            limit = MAX_XML if scope.get("path") == "/api/import" else 2 * MAX_UPLOAD + 65536

            async def limited_receive():
                nonlocal received
                message = await receive()
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "Envio excede o limite permitido.")
                return message

            await self.app(scope, limited_receive, send)

    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        if request.method in {"POST", "PUT"}:
            length = request.headers.get("content-length")
            if length is None:
                return JSONResponse({"detail": "Informe Content-Length no envio."}, status_code=411)
            try:
                limit = MAX_XML if request.url.path == "/api/import" else 2 * MAX_UPLOAD + 65536
                if not 0 <= int(length) <= limit:
                    raise ValueError
            except ValueError:
                return JSONResponse({"detail": "Envio grande demais ou tamanho inválido."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(FileNotFoundError)
    async def missing(_request, _exc):
        return JSONResponse({"detail": "Projeto ou arquivo não encontrado."}, status_code=404)

    def ensure_idle(project_id):
        if project_id in store.active:
            raise HTTPException(409, "Aguarde a renderização atual terminar.")

    def reset_render(project):
        project["render"] = {"status": "idle", "video_url": None, "error": None}
        (store.path(project["id"]) / "video.mp4").unlink(missing_ok=True)

    def build(image_data: bytes, audio_data: bytes, settings=None, rect=None):
        with store.lock:
            if len(store.building) >= 2:
                raise HTTPException(429, "Há análises em andamento. Tente novamente em instantes.")
            project_id, path = store.create()
        try:
            width, height = normalize_image(image_data, path / "image.png")
            normalize_audio(audio_data, path / "audio.wav")
            analysis = analyze_audio(path / "audio.wav")
            try:
                segmentation = segment_image(path / "image.png", path / "foreground.png", rect)
            except ValueError:
                if rect is not None:
                    raise
                segmentation = {"method": "none", "available": False, "rect": None,
                                "warning": "Não foi possível recortar automaticamente. Selecione uma região manualmente."}
            selected = Settings.model_validate(settings or {"bpm": analysis["bpm"]}).model_dump()
            if selected["mode"] == "subject" and not segmentation["available"]:
                raise ValueError("Selecione manualmente o personagem antes de usar este modo.")
            project = {
                "id": project_id, "width": width, "height": height,
                "duration": analysis.pop("duration"), "analysis": analysis,
                "segmentation": segmentation, "settings": selected,
                "image_url": f"/api/projects/{project_id}/image",
                "foreground_url": f"/api/projects/{project_id}/foreground" if segmentation["available"] else None,
            }
            project["keyframes"] = frames_for(project)
            reset_render(project)
            with store.lock:
                store.write(project)
            return public(project)
        except Exception:
            with store.lock:
                shutil.rmtree(path, ignore_errors=True)
            raise
        finally:
            with store.lock:
                store.building.discard(project_id)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "ffmpeg": bool(shutil.which("ffmpeg"))}

    @app.post("/api/projects", status_code=201)
    def upload(image: UploadFile, audio: UploadFile):
        return build(read_upload(image, MAX_UPLOAD), read_upload(audio, MAX_UPLOAD))

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        with store.lock:
            return public(store.read(project_id))

    @app.delete("/api/projects/{project_id}", status_code=204)
    def delete_project(project_id: str):
        store.remove(project_id)
        return Response(status_code=204)

    @app.put("/api/projects/{project_id}/settings")
    def update_settings(project_id: str, settings: Settings):
        with store.lock:
            ensure_idle(project_id)
            project = store.read(project_id)
            if settings.mode == "subject" and not project["segmentation"]["available"]:
                raise ValueError("Selecione uma cabeça ou personagem primeiro.")
            project["settings"] = settings.model_dump()
            project["keyframes"] = frames_for(project)
            reset_render(project)
            store.write(project)
            return public(project)

    @app.post("/api/projects/{project_id}/segment")
    def segment(project_id: str, selection: Selection):
        with store.lock:
            ensure_idle(project_id)
            project = store.read(project_id)
            path = store.path(project_id)
            project["segmentation"] = segment_image(path / "image.png", path / "foreground.png", selection.rect)
            project["foreground_url"] = f"/api/projects/{project_id}/foreground"
            reset_render(project)
            store.write(project)
            return public(project)

    def render_job(project_id):
        project = None
        try:
            with store.lock:
                project = store.read(project_id)
                project["render"]["status"] = "rendering"
                store.write(project)
            path = store.path(project_id)
            render_video(path / "image.png", path / "foreground.png" if project["segmentation"]["available"] else None,
                         path / "audio.wav", path / "video.mp4", project["keyframes"],
                         project["duration"], project["settings"]["mode"])
            project["render"] = {"status": "ready", "video_url": f"/api/projects/{project_id}/video", "error": None}
        except Exception:
            LOGGER.exception("Video rendering failed for project %s", project_id)
            if project is not None:
                project["render"] = {"status": "error", "video_url": None,
                                     "error": "Não foi possível renderizar. Verifique FFmpeg e tente novamente."}
        finally:
            with store.lock:
                try:
                    if project is not None:
                        store.write(project)
                finally:
                    store.active.discard(project_id)

    @app.post("/api/projects/{project_id}/render", status_code=202)
    def begin_render(project_id: str):
        with store.lock:
            if pool is None:
                raise HTTPException(503, "Servidor ainda está iniciando.")
            project = store.read(project_id)
            if project_id in store.active or project["render"]["status"] == "ready":
                return project["render"]
            if len(store.active) >= 2:
                raise HTTPException(429, "Fila de renderização cheia. Tente novamente em instantes.")
            project["render"] = {"status": "queued", "video_url": None, "error": None}
            store.write(project)
            store.active.add(project_id)
            pool.submit(render_job, project_id)
            return project["render"]

    @app.get("/api/projects/{project_id}/render")
    def render_status(project_id: str):
        with store.lock:
            project = store.read(project_id)
            if project["render"]["status"] in {"queued", "rendering"} and project_id not in store.active:
                reset_render(project)
                store.write(project)
            return project["render"]

    @app.get("/api/projects/{project_id}/video")
    def video(project_id: str, download: bool = False):
        project = store.read(project_id)
        path = store.path(project_id) / "video.mp4"
        if project["render"]["status"] != "ready" or not path.is_file():
            raise HTTPException(409, "Renderize o vídeo primeiro.")
        return FileResponse(path, media_type="video/mp4", filename="headbop.mp4" if download else None)

    @app.get("/api/projects/{project_id}/image")
    def image(project_id: str):
        return FileResponse(store.path(project_id) / "image.png", media_type="image/png")

    @app.get("/api/projects/{project_id}/foreground")
    def foreground(project_id: str):
        path = store.path(project_id) / "foreground.png"
        if not path.is_file():
            raise HTTPException(404, "Nenhum recorte disponível.")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/projects/{project_id}/export")
    def export(project_id: str):
        with store.lock:
            project = store.read(project_id)
            path = store.path(project_id)
            data = export_project(public(project), (path / "image.png").read_bytes(), (path / "audio.wav").read_bytes())
        return Response(data, media_type="application/xml", headers={"Content-Disposition": 'attachment; filename="headbop.xml"'})

    @app.post("/api/import", status_code=201)
    def import_xml(file: UploadFile):
        parsed = import_project(read_upload(file, MAX_XML - 65536))
        return build(parsed["image"], parsed["audio"], parsed["settings"], parsed.get("rect"))

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
    return app


app = create_app()
