"""HTTP application and bounded local processing pipeline."""

import io
import logging
import os
import re
import shutil
import subprocess
import threading
import tempfile
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from .animation import generate_keyframes, generate_motion
from .audio import analyze_audio
from .formats import export_project, import_project
from .imaging import segment_image
from .painting import PaintSelection, paint_selection
from .auth import AuthBusyError, AuthStore, InvalidCredentialsError, normalize_username
from .jobs import run_render_job
from .security import (Admission, COOKIE, SAFE_METHODS, SecurityConfig, check_csrf, fail,
                       network_scope)
from .rigging import build_rig
from .settings import Settings, effective_settings
from .store import MIB, QuotaError, Store

MAX_UPLOAD = 20 * 1024 * 1024
MAX_XML = 64 * 1024 * 1024
LOGGER = logging.getLogger("headbop")


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=128)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    rect: list[float] = Field(min_length=4, max_length=4)


class HeadSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    head_rect: list[float] = Field(min_length=4, max_length=4)


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
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
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
    settings = effective_settings(project["settings"])
    if settings["engine"] == "v2":
        return generate_motion(project["duration"], project["analysis"], settings, project.get("rig"))
    return generate_keyframes(project["duration"], project["analysis"]["beats"],
                              project["analysis"]["intensity"], settings["bpm"],
                              settings["intensity"], settings["motion"])


def public(project: dict) -> dict:
    result = {key: value for key, value in project.items() if key not in {"render", "owner_id"}}
    result["settings"] = effective_settings(project["settings"])
    result["mask_url"] = project.get("foreground_url")
    result["capabilities"] = {"reference_available": False}
    result.setdefault("rig", {"available": False, "kind": "whole", "confidence": 0,
                              "warning": "Projeto original; ative o motor 2.0 para preparar camadas."})
    return result


def create_app(data_dir: Path | None = None) -> FastAPI:
    store = Store(data_dir or Path(os.getenv("HEADBOP_DATA_DIR", "/tmp/headbop-ai")))
    auth = AuthStore(store.root / "security.sqlite3")
    config = SecurityConfig.from_env()
    admission = Admission()
    pool = None
    stop = threading.Event()
    progress_lock = threading.Lock()
    # Progress is transient and scoped to the current single-worker server.
    render_progress: dict[str, int] = {}

    def cleanup_loop():
        while not stop.wait(300):
            store.cleanup()
            auth.cleanup(store.owner_ids())

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal pool, stop
        config.validate()
        with store.instance():
            store.recover()
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="headbop-render")
            stop = threading.Event()
            store.cleanup()
            auth.cleanup(store.owner_ids())
            cleaner = threading.Thread(target=cleanup_loop, daemon=True)
            cleaner.start()
            try:
                yield
            finally:
                stop.set()
                pool.shutdown(wait=True)
                cleaner.join(timeout=1)
                pool = None

    app = FastAPI(title="Headbop AI", version="2.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.auth = auth
    app.state.config = config
    app.state.admission = admission

    def body_limit(path):
        if path == "/api/import":
            return MAX_XML
        if path == "/api/projects":
            return 2 * MAX_UPLOAD + 65536
        if path.endswith("/paint"):
            return 256 * 1024
        return 65536

    class BodyLimit:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            received = 0
            limit = body_limit(scope.get("path", ""))

            async def limited_receive():
                nonlocal received
                message = await receive()
                received += len(message.get("body", b""))
                if received > limit:
                    fail(413, "upload_too_large")
                return message

            await self.app(scope, limited_receive, send)

    app.add_middleware(BodyLimit)

    def error_response(exc):
        payload = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
        return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)

    def consume(key, limit, seconds):
        retry = auth.consume_rate(key, limit, seconds)
        if retry:
            fail(429, "rate_limited", seconds=retry)

    def guard(request):
        path = request.url.path
        if not (path.startswith("/api/") or path == "/openapi.json") or path == "/api/health":
            return
        config.request_origin(request)
        peer = network_scope(request.client.host if request.client else "unknown")
        request.state.peer = peer
        # Only the ASGI server's explicitly trusted proxy can supply client IPs.
        # Raw X-Forwarded-For never participates in identity/rate calculations.
        consume("api-global", 3000, 60)
        consume("api-ip:" + peer, 600, 60)
        if request.method not in SAFE_METHODS:
            config.check_origin(request)
        if path == "/api/auth/login" and request.method == "POST":
            consume("login-global", 60, 60)
            consume("login-ip:" + peer, 10, 600)
        elif path == "/api/auth/guest" and request.method == "POST":
            # Existing browsers keep their workspace and do not consume the
            # creation quota on refresh, navigation or a second tab.
            request.state.session = auth.get_session(request.cookies.get(COOKIE))
            if request.state.session is None:
                consume("guest-global", 100, 300)
                consume("guest-ip:" + peer, 10, 3600)
        else:
            session = auth.get_session(request.cookies.get(COOKIE))
            if session is None:
                fail(401, "auth_required")
            request.state.session = session
            request.state.user = session["user"]
            consume("api-user:" + session["user"]["id"], 600, 60)
            if request.method not in SAFE_METHODS:
                check_csrf(request, session)
            match = re.match(r"^/api/projects/([^/]+)", path)
            if match:
                try:
                    store.authorize(match[1], session["user"]["id"])
                except FileNotFoundError:
                    fail(404, "project_not_found")
        if request.method in {"POST", "PUT", "PATCH"}:
            length = request.headers.get("content-length")
            if length is None:
                fail(411, "content_length_required")
            try:
                if not 0 <= int(length) <= body_limit(path):
                    raise ValueError
            except ValueError:
                fail(413, "upload_too_large")
        creating = request.method == "POST" and path in {"/api/projects", "/api/import"}
        editing = request.method in {"POST", "PUT"} and path.endswith(("/segment", "/rig", "/settings", "/paint"))
        if creating or editing:
            user_id = request.state.user["id"]
            consume("analysis-user:" + user_id, 60, 3600)
            guest_scope = auth.guest_scope(user_id)
            admission_key = "guest:" + guest_scope if guest_scope else user_id
            if guest_scope:
                consume("analysis-guest:" + guest_scope, 60, 3600)
                consume("analysis-ip:" + peer, 60, 3600)
            admission.enter(admission_key)
            request.state.admitted = admission_key
            try:
                reserved_size = 128 * MIB if creating else (64 if path.endswith("/paint") else 32) * MIB
                with store.lock:
                    # A new browser may have consumed the shared budget before
                    # admission. Resolve owners atomically with the reservation.
                    quota_owners = auth.guest_owners(user_id)
                    request.state.reservation = store.reserve(user_id, reserved_size,
                                                              projects=1 if creating else 0,
                                                              quota_owners=quota_owners)
            except QuotaError as exc:
                fail(429, exc.code, seconds=60)

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        try:
            await run_in_threadpool(guard, request)
            response = await call_next(request)
        except HTTPException as exc:
            response = error_response(exc)
        finally:
            if token := getattr(request.state, "reservation", None):
                await run_in_threadpool(store.release, token)
            if user_id := getattr(request.state, "admitted", None):
                admission.leave(user_id)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        if config.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_schema(_request, _exc):
        return JSONResponse({"code": "invalid_request", "params": {}}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        if isinstance(exc, QuotaError):
            return JSONResponse({"code": exc.code, "params": {}}, status_code=429,
                                headers={"Retry-After": "60"})
        return JSONResponse({"code": "invalid_request", "detail": str(exc)}, status_code=422)

    @app.exception_handler(FileNotFoundError)
    async def missing(_request, _exc):
        return JSONResponse({"code": "project_not_found"}, status_code=404)

    @app.post("/api/auth/login")
    def login(credentials: Login):
        try:
            username_bucket = normalize_username(credentials.username)
        except ValueError:
            username_bucket = "!invalid"
        consume("login-user:" + username_bucket, 10, 600)
        try:
            session = auth.login(credentials.username, credentials.password)
        except AuthBusyError:
            fail(429, "auth_busy", seconds=2)
        except (ValueError, InvalidCredentialsError):
            session = None
        if session is None:
            fail(401, "invalid_credentials")
        response = JSONResponse({key: session[key] for key in ("user", "csrf", "expires_at")})
        response.set_cookie(COOKIE, session["token"], max_age=int(auth.session_ttl_seconds),
                            httponly=True, secure=config.secure_cookie, samesite="strict", path="/")
        return response

    @app.get("/api/auth/session")
    def session(request: Request):
        return request.state.session

    @app.post("/api/auth/guest")
    def guest_workspace(request: Request):
        if request.state.session is not None:
            return request.state.session
        session = auth.create_guest(request.state.peer)
        response = JSONResponse({key: session[key] for key in ("user", "csrf", "expires_at")})
        response.set_cookie(COOKIE, session["token"], max_age=int(auth.session_ttl_seconds),
                            httponly=True, secure=config.secure_cookie, samesite="strict", path="/")
        return response

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request):
        auth.revoke_session(request.cookies.get(COOKIE))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/", secure=config.secure_cookie,
                               httponly=True, samesite="strict")
        return response

    @app.get("/api/projects")
    def list_projects(request: Request):
        return {"projects": store.list_owned(request.state.user["id"])}

    def ensure_idle(project_id):
        if project_id in store.active:
            raise HTTPException(409, "Aguarde a renderização atual terminar.")

    def reset_render(project):
        with progress_lock:
            render_progress.pop(project["id"], None)
        project["render"] = {"status": "idle", "video_url": None, "error": None}
        (store.path(project["id"]) / "video.mp4").unlink(missing_ok=True)

    def check_ready_output(project):
        """A persisted ready flag cannot outlive a missing/truncated output file."""
        if project["render"]["status"] != "ready":
            return
        output = store.path(project["id"]) / "video.mp4"
        if not output.is_file() or output.is_symlink() or output.stat().st_size == 0:
            project["render"] = {"status": "error", "video_url": None,
                                 "error": "render_failed", "code": "render_failed"}
            store.write(project)

    def prepare_rig(project, path, head_rect=None):
        # Old stored projects have no anatomy/confidence metadata. Add conservative
        # defaults only when explicitly rebuilding a rig, never on a read.
        segmentation = {"kind": "unknown", "confidence": 0.0, "head_rect": None,
                        **project["segmentation"]}
        project["segmentation"] = segmentation
        project["rig"] = build_rig(path / "image.png",
                                   path / "foreground.png" if segmentation["available"] else None,
                                   path, segmentation, head_rect)

    def build(image_data: bytes, audio_data: bytes, settings=None, rect=None,
              head_rect=None, restored_frames=None, restored_duration=None,
              foreground_data=None, segmentation_info=None, *, owner_id, reservation):
        with store.lock:
            if len(store.building) >= 2:
                raise HTTPException(429, "Há análises em andamento. Tente novamente em instantes.")
            project_id, path = store.create(owner_id, reservation)
        try:
            width, height = normalize_image(image_data, path / "image.png")
            normalize_audio(audio_data, path / "audio.wav")
            analysis = analyze_audio(path / "audio.wav")
            if foreground_data is not None:
                mask_size = normalize_image(foreground_data, path / "foreground.png")
                if mask_size != (width, height) or segmentation_info is None:
                    raise ValueError("O recorte incorporado não corresponde à imagem do projeto.")
                segmentation = {**segmentation_info, "available": True, "rect": rect,
                                "warning": "Recorte original preservado do projeto XML."}
            else:
                try:
                    segmentation = segment_image(path / "image.png", path / "foreground.png", rect)
                except ValueError:
                    if rect is not None:
                        raise
                    segmentation = {"method": "none", "available": False, "rect": None,
                                    "kind": "unknown", "confidence": 0.0, "head_rect": None,
                                    "warning": "Não foi possível recortar automaticamente. Selecione uma região manualmente."}
            default_mode = "subject" if (segmentation["available"] and
                                           segmentation.get("confidence", 0) >= 0.55) else "whole"
            selected = Settings.model_validate(settings or {"bpm": analysis["bpm"], "mode": default_mode}).model_dump()
            if selected["mode"] == "subject" and not segmentation["available"]:
                raise ValueError("Selecione manualmente o personagem antes de usar este modo.")
            project = {
                "id": project_id, "width": width, "height": height,
                "duration": analysis.pop("duration"), "analysis": analysis,
                "segmentation": segmentation, "settings": selected,
                "image_url": f"/api/projects/{project_id}/image",
                "foreground_url": f"/api/projects/{project_id}/foreground" if segmentation["available"] else None,
            }
            if selected["engine"] == "v2":
                prepare_rig(project, path, head_rect)
            if restored_frames is not None:
                if restored_duration is None or abs(restored_duration - project["duration"]) > 1 / 22050 + 1e-6:
                    raise ValueError("A duração da timeline XML não corresponde ao áudio incorporado.")
                project["keyframes"] = restored_frames
            else:
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
    def upload(request: Request, image: UploadFile, audio: UploadFile):
        return build(read_upload(image, MAX_UPLOAD), read_upload(audio, MAX_UPLOAD),
                     owner_id=request.state.user["id"], reservation=request.state.reservation)

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
            selected = Settings.model_validate({**effective_settings(project["settings"]),
                                                **settings.model_dump(exclude_unset=True)}).model_dump()
            if selected["mode"] == "subject" and not project["segmentation"]["available"]:
                raise ValueError("Selecione uma cabeça ou personagem primeiro.")
            project["settings"] = selected
            if selected["engine"] == "v2" and "rig" not in project:
                prepare_rig(project, store.path(project_id))
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
            prepare_rig(project, path)
            project["keyframes"] = frames_for(project)
            reset_render(project)
            store.write(project)
            return public(project)

    @app.post("/api/projects/{project_id}/paint")
    def paint(project_id: str, selection: PaintSelection):
        with store.lock:
            ensure_idle(project_id)
            project = store.read(project_id)
            path = store.path(project_id)
            # Prepare every dependent asset before replacing any visible file.
            # Backups are hard links on this same filesystem, so preserving a
            # previous large video does not consume another video's quota.
            with tempfile.TemporaryDirectory(prefix=".paint-", dir=path) as temporary:
                staged = Path(temporary)
                project["segmentation"] = paint_selection(
                    path / "image.png", staged / "foreground.png", selection,
                    path / "foreground.png" if project["segmentation"]["available"] else None)
                project["foreground_url"] = f"/api/projects/{project_id}/foreground"
                project["settings"] = {**effective_settings(project["settings"]), "mode": "subject"}
                project["rig"] = build_rig(path / "image.png", staged / "foreground.png",
                                           staged, project["segmentation"])
                project["keyframes"] = frames_for(project)
                project["render"] = {"status": "idle", "video_url": None, "error": None}
                assets = ["foreground.png", *project["rig"]["layers"].values()]
                names = [*assets, "project.json", "video.mp4"]
                backups = staged / "backups"
                backups.mkdir()
                for name in names:
                    if (path / name).is_file():
                        os.link(path / name, backups / name)
                try:
                    for name in assets:
                        (staged / name).replace(path / name)
                    store.write(project)
                    reset_render(project)
                except Exception:
                    for name in names:
                        if (backups / name).is_file():
                            (backups / name).replace(path / name)
                        else:
                            (path / name).unlink(missing_ok=True)
                    (path / "project.json.tmp").unlink(missing_ok=True)
                    raise
            return public(project)

    @app.post("/api/projects/{project_id}/rig")
    def adjust_rig(project_id: str, selection: HeadSelection):
        with store.lock:
            ensure_idle(project_id)
            project = store.read(project_id)
            if effective_settings(project["settings"])["engine"] != "v2":
                raise ValueError("Ative o motor 2.0 para ajustar cabeça e pivôs.")
            path = store.path(project_id)
            if not project["segmentation"]["available"]:
                project["segmentation"] = segment_image(path / "image.png", path / "foreground.png",
                                                          selection.head_rect)
                project["foreground_url"] = f"/api/projects/{project_id}/foreground"
            prepare_rig(project, path, selection.head_rect)
            project["keyframes"] = frames_for(project)
            reset_render(project)
            store.write(project)
            return public(project)

    def publish_progress(project_id: str, completed: int, total: int) -> None:
        # Avoid writing project.json for every rendered frame.
        with progress_lock:
            render_progress[project_id] = min(95, 5 + round(90 * completed / max(1, total)))

    def render_response(project):
        result = dict(project["render"])
        with progress_lock:
            result["progress"] = 100 if result["status"] == "ready" else render_progress.get(project["id"], 0)
        # One running job and one waiting job can consume two complete budgets.
        # Clients establish a fixed deadline after POST, never extend it on polls.
        result["timeout_seconds"] = 2 * config.render_timeout + 30
        return result

    def render_job(project_id, reservation):
        project = None
        try:
            with store.lock:
                project = store.read(project_id)
                project["render"]["status"] = "rendering"
                store.write(project)
            path = store.path(project_id)
            run_render_job(path, timeout_seconds=config.render_timeout,
                           max_output_bytes=config.video_bytes, cancel_event=stop,
                           progress_callback=lambda done, total: publish_progress(project_id, done, total))
            project["render"] = {"status": "ready", "video_url": f"/api/projects/{project_id}/video", "error": None}
        except Exception as exc:
            LOGGER.exception("Video rendering failed for project %s", project_id)
            if project is not None:
                code = "render_timeout" if isinstance(exc, TimeoutError) else "render_failed"
                project["render"] = {"status": "error", "video_url": None, "error": code, "code": code}
        finally:
            with store.lock:
                try:
                    if project is not None:
                        store.write(project)
                finally:
                    with progress_lock:
                        render_progress.pop(project_id, None)
                    store.active.discard(project_id)
                    store.release(reservation)

    @app.post("/api/projects/{project_id}/render", status_code=202)
    def begin_render(project_id: str, request: Request):
        with store.lock:
            if pool is None or stop.is_set():
                fail(503, "capacity_busy", seconds=2)
            project = store.read(project_id)
            check_ready_output(project)
            if project_id in store.active or project["render"]["status"] == "ready":
                return render_response(project)
            owner_id = request.state.user["id"]
            quota_owners = auth.guest_owners(owner_id)
            if len(store.active) >= 2 or any(store.owner(pid) in quota_owners for pid in store.active):
                fail(429, "capacity_busy", seconds=2)
            consume("render-user:" + owner_id, 30, 3600)
            if guest_scope := auth.guest_scope(owner_id):
                consume("render-guest:" + guest_scope, 30, 3600)
                consume("render-ip:" + request.state.peer, 30, 3600)
            reservation = store.reserve(owner_id, config.video_bytes, quota_owners=quota_owners)
            try:
                project["render"] = {"status": "queued", "video_url": None, "error": None}
                with progress_lock:
                    render_progress[project_id] = 0
                store.write(project)
                store.active.add(project_id)
                pool.submit(render_job, project_id, reservation)
            except Exception:
                store.active.discard(project_id)
                store.release(reservation)
                raise
            return render_response(project)

    @app.get("/api/projects/{project_id}/render")
    def render_status(project_id: str):
        with store.lock:
            project = store.read(project_id)
            check_ready_output(project)
            if project["render"]["status"] in {"queued", "rendering"} and project_id not in store.active:
                project["render"] = {"status": "error", "video_url": None,
                                     "error": "render_failed", "code": "render_failed"}
                store.write(project)
            return render_response(project)

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
            portable = public(project)
            # V1 stores only a rectangular selection. A painted mask requires
            # V2's embedded foreground, which also supports the legacy engine.
            if (portable["settings"]["engine"] == "legacy"
                    and project["segmentation"].get("method") not in {"smart-brush", "manual-brush"}):
                portable["settings"] = {key: portable["settings"][key]
                                        for key in ("mode", "motion", "bpm", "intensity")}
            foreground_data = (path / "foreground.png").read_bytes() if project["segmentation"]["available"] else None
            data = export_project(portable, (path / "image.png").read_bytes(), (path / "audio.wav").read_bytes(),
                                  foreground_bytes=foreground_data)
        return Response(data, media_type="application/xml", headers={"Content-Disposition": 'attachment; filename="headbop.xml"'})

    @app.post("/api/import", status_code=201)
    def import_xml(request: Request, file: UploadFile):
        parsed = import_project(read_upload(file, MAX_XML - 65536))
        selected = parsed["settings"]
        if parsed["schema_version"] == 1:
            selected = {**selected, "engine": "legacy"}
        return build(parsed["image"], parsed["audio"], selected, parsed.get("rect"),
                     parsed.get("head_rect"), parsed["keyframes"], parsed["duration"],
                     parsed.get("foreground"), parsed.get("segmentation_info"),
                     owner_id=request.state.user["id"], reservation=request.state.reservation)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
    return app


app = create_app()
