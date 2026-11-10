"""Deterministic CPU frames piped to FFmpeg. Preview/download share this MP4."""

from __future__ import annotations

from dataclasses import dataclass
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .jobs import _kill_process_tree, _process_group_options, render_timeout

FPS = 24
MAX_EDGE = 640
MAX_FRAMES = 1800
BACKGROUND = np.array([18, 20, 27], dtype=np.float32)
_TRANSFORM_FIELDS = ("x", "y", "rotation", "scale", "scale_x", "scale_y")
_IDENTITY = {"x": 0.0, "y": 0.0, "rotation": 0.0,
             "scale": 1.0, "scale_x": 1.0, "scale_y": 1.0}
_RIG_FILES = {"background": "rig-background.png", "body": "rig-body.png", "head": "rig-head.png"}


def _load_rgba(path: Path, size: tuple[int, int]) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA").resize(size, Image.Resampling.LANCZOS)).copy()


def _premultiply(layer: np.ndarray) -> np.ndarray:
    alpha = layer[:, :, 3:4].astype(np.float32) / 255
    return np.concatenate((layer[:, :, :3].astype(np.float32) * alpha, alpha), axis=2)


def _mix_transform(left: dict, right: dict, fraction: float) -> dict:
    return {field: left.get(field, _IDENTITY[field]) + fraction *
            (right.get(field, _IDENTITY[field]) - left.get(field, _IDENTITY[field]))
            for field in _TRANSFORM_FIELDS}


def _frame_transform(keyframes: list[dict], timestamp: float) -> dict:
    # Sparse imported timelines interpolate every transform, including children.
    times = np.asarray([frame["time"] for frame in keyframes])
    index = int(np.searchsorted(times, timestamp, side="right"))
    if index == 0:
        return keyframes[0]
    if index == len(keyframes):
        return keyframes[-1]
    left, right = keyframes[index - 1], keyframes[index]
    fraction = (timestamp - left["time"]) / (right["time"] - left["time"])
    result = _mix_transform(left, right, fraction)
    if "layers" in left or "layers" in right:
        result["layers"] = {
            name: _mix_transform(left.get("layers", {}).get(name, left if name == "body" else {}),
                                 right.get("layers", {}).get(name, right if name == "body" else {}),
                                 fraction)
            for name in ("body", "head")
        }
    return result


def _options(settings: dict | None) -> tuple[int, int, str, bool]:
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("Configuração de render inválida.")
    selected = settings or {}
    fps = selected.get("fps", FPS)
    resolution = selected.get("resolution", MAX_EDGE)
    aspect = selected.get("aspect", "source")
    engine = selected.get("engine", "v2" if settings is not None else "legacy")
    if type(fps) is not int or fps not in (24, 30, 60):
        raise ValueError("FPS deve ser 24, 30 ou 60.")
    if type(resolution) is not int or resolution not in (640, 720, 1080):
        raise ValueError("Resolução deve ser 640, 720 ou 1080.")
    if aspect not in ("source", "square", "vertical") or engine not in ("legacy", "v2"):
        raise ValueError("Formato ou motor de render inválido.")
    return fps, resolution, aspect, engine == "legacy"


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_transform(transform: dict) -> None:
    if not isinstance(transform, dict):
        raise ValueError("Keyframe inválido.")
    if any(field not in transform for field in ("x", "y", "rotation", "scale")):
        raise ValueError("Keyframe inválido.")
    if any(not _finite_number(transform.get(field, _IDENTITY[field])) for field in _TRANSFORM_FIELDS):
        raise ValueError("Keyframe inválido.")
    if (any(not 0.1 <= transform.get(field, 1.0) <= 4 for field in ("scale", "scale_x", "scale_y"))
            or abs(transform["x"]) > 2 or abs(transform["y"]) > 2
            or abs(transform["rotation"]) > 360):
        raise ValueError("Transformação fora dos limites.")


def _validate_timeline(keyframes: list[dict], duration: float, fps: int, legacy: bool) -> None:
    if not _finite_number(duration) or not 0.5 <= duration <= 30:
        raise ValueError("Duração deve estar entre 0,5 e 30 segundos.")
    if math.ceil(duration * fps) > MAX_FRAMES:
        raise ValueError("Render excede 1800 frames.")
    # The last timeline sample is the exact endpoint, not an additional encoded frame.
    if not isinstance(keyframes, list) or not keyframes or len(keyframes) > (1000 if legacy else MAX_FRAMES + 1):
        raise ValueError("Linha do tempo inválida.")
    previous_time = -1.0
    for frame in keyframes:
        _validate_transform(frame)
        time = frame.get("time")
        if not _finite_number(time):
            raise ValueError("Keyframe inválido.")
        if time <= previous_time or time < 0 or time > duration + 1e-6:
            raise ValueError("Tempos dos keyframes devem estar em ordem crescente.")
        if "layers" in frame:
            layers = frame["layers"]
            if not isinstance(layers, dict) or set(layers) != {"body", "head"}:
                raise ValueError("Camadas do keyframe inválidas.")
            for transform in layers.values():
                _validate_transform(transform)
        previous_time = time


def _sizes(source: tuple[int, int], resolution: int, aspect: str,
           legacy: bool) -> tuple[tuple[int, int], tuple[int, int]]:
    if aspect == "source":
        if resolution in (720, 1080) and not legacy:
            long_edge = 1280 if resolution == 720 else 1920
            factor = min(resolution / min(source), long_edge / max(source))
        else:
            factor = resolution / max(source)
        if legacy:
            factor = min(1.0, factor)
        size = tuple(max(2, int(side * factor) // 2 * 2) for side in source)
        return size, size
    if aspect == "square":
        output = (resolution, resolution)
    elif resolution in (720, 1080) and not legacy:
        output = (resolution, 1280 if resolution == 720 else 1920)
    else:
        output = (resolution * 9 // 16 // 2 * 2, resolution)
    factor = min(output[0] / source[0], output[1] / source[1])
    fitted = tuple(max(1, min(output[i], round(source[i] * factor))) for i in range(2))
    return output, fitted


@dataclass
class _Scene:
    size: tuple[int, int]
    content_size: tuple[int, int]
    offset: tuple[int, int]
    background: np.ndarray
    body: np.ndarray
    head: np.ndarray | None
    body_pivot: tuple[float, float]
    head_pivot: tuple[float, float]
    legacy: bool


def _rig_assets(image_path: Path, source_size: tuple[int, int], content_size: tuple[int, int],
                rig: dict | None) -> tuple[dict, dict] | None:
    if not isinstance(rig, dict) or not rig.get("available") or rig.get("kind") not in ("single", "head-body"):
        return None
    assets = {}
    pivots = rig.get("pivots", {})
    for name in ("body", "head"):
        pivot = pivots.get(name) if isinstance(pivots, dict) else None
        if (not isinstance(pivot, (tuple, list)) or len(pivot) != 2
                or any(not _finite_number(value) or not 0 <= value <= 1 for value in pivot)):
            raise ValueError("Pivô do rig inválido.")
    # Imported/user metadata never chooses a server path. Only our fixed files are read.
    parent = image_path.parent.resolve()
    for name, basename in _RIG_FILES.items():
        path = parent / basename
        if not path.is_file() or path.resolve().parent != parent:
            return None
        with Image.open(path) as image:
            if image.size != source_size:
                raise ValueError("As camadas do rig devem ter o tamanho da imagem.")
        assets[name] = _load_rgba(path, content_size)
    return assets, pivots


def _prepare_scene(image_path: Path, foreground_path: Path | None, mode: str,
                   settings: dict | None = None, rig: dict | None = None) -> _Scene:
    _, resolution, aspect, legacy = _options(settings)
    with Image.open(image_path) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("Imagem excede 16 megapixels.")
        source_size = image.size
    size, content_size = _sizes(source_size, resolution, aspect, legacy)
    width, height = content_size
    offset = ((size[0] - width) // 2, (size[1] - height) // 2)
    original = _load_rgba(image_path, content_size)
    original_alpha = original[:, :, 3:4].astype(np.float32) / 255
    flattened = original[:, :, :3] * original_alpha + BACKGROUND * (1 - original_alpha)
    head = None
    head_pivot = (width / 2, height / 2)
    assets = _rig_assets(image_path, source_size, content_size, rig) if mode == "subject" and not legacy else None
    if assets is not None:
        layers, pivots = assets
        bg = _premultiply(layers["background"])
        background = bg[:, :, :3] + BACKGROUND * (1 - bg[:, :, 3:4])
        layer = layers["body"]
        center = (pivots["body"][0] * width, pivots["body"][1] * height)
        if rig["kind"] == "head-body":
            head = _premultiply(layers["head"])
            head_pivot = (pivots["head"][0] * width, pivots["head"][1] * height)
    elif mode == "subject":
        if foreground_path is None or not foreground_path.is_file():
            raise ValueError("Faça o recorte antes de animar a cabeça/personagem.")
        layer = _load_rgba(foreground_path, content_size)
        mask = (layer[:, :, 3] > 8).astype(np.uint8) * 255
        if not np.any(mask):
            raise ValueError("O recorte está vazio.")
        if np.any(original[:, :, 3] < 250):
            background = np.broadcast_to(BACKGROUND, (height, width, 3)).copy()
        else:
            mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
            background = cv2.inpaint(flattened.astype(np.uint8), mask, 3, cv2.INPAINT_TELEA).astype(np.float32)
        ys, xs = np.nonzero(layer[:, :, 3] > 8)
        center = (float(xs.mean()), float(ys.mean()))
    else:
        layer = original
        background = np.broadcast_to(BACKGROUND, (height, width, 3)).copy()
        center = (width / 2, height / 2)
    if size != content_size:
        fitted_background = np.broadcast_to(BACKGROUND, (size[1], size[0], 3)).copy()
        fitted_background[offset[1]:offset[1] + height, offset[0]:offset[0] + width] = background
        background = fitted_background
    return _Scene(size, content_size, offset, background, _premultiply(layer), head,
                  center, head_pivot, legacy)


def _matrix(transform: dict, pivot: tuple[float, float], size: tuple[int, int],
            legacy: bool = False) -> np.ndarray:
    rotation = cv2.getRotationMatrix2D((0, 0), transform["rotation"], transform["scale"])
    if not legacy:
        # Local squash/stretch happens before rotation. Each axis is a scale multiplier.
        rotation[:, 0] *= transform.get("scale_x", 1.0)
        rotation[:, 1] *= transform.get("scale_y", 1.0)
    matrix = np.eye(3, dtype=np.float64)
    matrix[:2, :2] = rotation[:, :2]
    matrix[:2, 2] = np.asarray(pivot) - matrix[:2, :2] @ pivot
    matrix[0, 2] += transform["x"] * size[0]
    matrix[1, 2] += transform["y"] * size[1]
    return matrix


def _render_frame(scene: _Scene, frame: dict) -> np.ndarray:
    body_transform = frame.get("layers", {}).get("body", frame) if scene.head is not None else frame
    body_matrix = _matrix(body_transform, scene.body_pivot, scene.content_size, scene.legacy)
    canvas = np.eye(3)
    canvas[:2, 2] = scene.offset
    transforms = [(scene.body, canvas @ body_matrix)]
    if scene.head is not None:
        head_transform = frame.get("layers", {}).get("head", _IDENTITY)
        transforms.append((scene.head, canvas @ body_matrix @
                           _matrix(head_transform, scene.head_pivot, scene.content_size)))
    result = scene.background
    for layer, matrix in transforms:
        warped = cv2.warpAffine(layer, matrix[:2], scene.size, flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        result = warped[:, :, :3] + result * (1 - warped[:, :, 3:4])
    return np.clip(result, 0, 255).astype(np.uint8)


def render_video(
    image_path: Path, foreground_path: Path | None, audio_path: Path,
    output_path: Path, keyframes: list[dict], duration: float, mode: str,
    *, settings: dict | None = None, rig: dict | None = None,
    progress_callback=None, timeout_seconds: float | None = None,
    isolate_process_group: bool = True,
) -> None:
    """Render H.264/AAC on CPU; old positional calls retain 640px/24fps behavior.

    V2 fits the source inside the selected canvas, uses normalized source-space
    pivots, and composes head motion relative to the body. At most 1800 frames
    are streamed to FFmpeg; no full timeline of image buffers is retained.
    A watchdog also interrupts blocked encoder writes. Production jobs run in
    a supervised worker so a native scene-composition hang is interruptible.
    """
    deadline = time.monotonic() + render_timeout(timeout_seconds)

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("O render excedeu o tempo limite.")
        return seconds

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg não está instalado no servidor.")
    if mode not in ("whole", "subject"):
        raise ValueError("Modo de animação inválido.")
    fps, _, _, legacy = _options(settings)
    _validate_timeline(keyframes, duration, fps, legacy)
    scene = _prepare_scene(image_path, foreground_path, mode, settings, rig)
    remaining()
    width, height = scene.size
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(output_path.name + ".partial.mp4")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(fps), "-i", "pipe:0", "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
        "-threads", "2", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-b:a", "128k", "-t", str(duration), "-movflags", "+faststart", str(partial),
    ]
    process = None
    timer = None
    published = False
    complete = False
    expired = threading.Event()

    def expire():
        expired.set()
        _kill_process_tree(process, isolate_process_group)

    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, bufsize=0,
                                   **_process_group_options(isolate_process_group))
        timer = threading.Timer(remaining(), expire)
        timer.daemon = True
        timer.start()
        assert process.stdin is not None
        try:
            total_frames = math.ceil(duration * fps)
            interval = max(1, total_frames // 100)
            for number in range(total_frames):
                remaining()
                frame = _frame_transform(keyframes, number / fps)
                data = memoryview(_render_frame(scene, frame).tobytes())
                while data:
                    remaining()
                    written = process.stdin.write(data)
                    if not written:
                        raise BrokenPipeError
                    data = data[written:]
                if progress_callback is not None and ((number + 1) % interval == 0 or number + 1 == total_frames):
                    progress_callback(number + 1, total_frames)
        except BrokenPipeError as error:
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("O render excedeu o tempo limite.") from error
            raise RuntimeError("FFmpeg interrompeu a codificação do vídeo.") from error
        finally:
            # Unbuffered writes leave nothing to flush during close.
            process.stdin.close()
        try:
            returncode = process.wait(timeout=remaining())
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("O render excedeu o tempo limite.") from error
        remaining()
        if expired.is_set():
            raise TimeoutError("O render excedeu o tempo limite.")
        if returncode != 0:
            raise RuntimeError("FFmpeg não conseguiu codificar o vídeo.")
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError("O vídeo gerado está vazio.")
        partial.replace(output_path)
        published = True
        remaining()
        complete = True
    finally:
        if timer is not None:
            timer.cancel()
            if timer.ident is not None:
                timer.join()
        if process is not None:
            _kill_process_tree(process, isolate_process_group)
            process.wait()
            if process.stdin is not None:
                process.stdin.close()
        partial.unlink(missing_ok=True)
        if published and not complete:
            output_path.unlink(missing_ok=True)
