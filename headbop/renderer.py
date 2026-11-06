"""Deterministic CPU frames piped to FFmpeg. Preview/download share this MP4."""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

FPS = 24
MAX_EDGE = 640
BACKGROUND = np.array([18, 20, 27], dtype=np.float32)


def _load_rgba(path: Path, size: tuple[int, int]) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA").resize(size, Image.Resampling.LANCZOS)).copy()


def _frame_transform(keyframes: list[dict], timestamp: float) -> dict:
    # Linear interpolation is only a guard for externally supplied sparse frames;
    # generated timelines contain a sample at each output frame timestamp.
    times = np.asarray([frame["time"] for frame in keyframes])
    index = int(np.searchsorted(times, timestamp, side="right"))
    if index == 0:
        return keyframes[0]
    if index == len(keyframes):
        return keyframes[-1]
    left, right = keyframes[index - 1], keyframes[index]
    fraction = (timestamp - left["time"]) / (right["time"] - left["time"])
    return {field: left[field] + fraction * (right[field] - left[field])
            for field in ("x", "y", "rotation", "scale")}


def render_video(
    image_path: Path, foreground_path: Path | None, audio_path: Path,
    output_path: Path, keyframes: list[dict], duration: float, mode: str,
) -> None:
    """Render up to 30s, 640px, 24fps, H.264/AAC, using only trusted local paths."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg não está instalado no servidor.")
    if not math.isfinite(duration) or duration < 0.5 or duration > 30:
        raise ValueError("Duração deve estar entre 0,5 e 30 segundos.")
    if mode not in ("whole", "subject"):
        raise ValueError("Modo de animação inválido.")
    if not keyframes or len(keyframes) > 1000:
        raise ValueError("Linha do tempo inválida.")
    previous_time = -1.0
    for frame in keyframes:
        if any(not math.isfinite(float(frame[field])) for field in ("time", "x", "y", "rotation", "scale")):
            raise ValueError("Keyframe inválido.")
        if frame["time"] <= previous_time or frame["time"] < 0 or frame["time"] > duration + 1e-6:
            raise ValueError("Tempos dos keyframes devem estar em ordem crescente.")
        if not 0.1 <= frame["scale"] <= 4 or abs(frame["x"]) > 2 or abs(frame["y"]) > 2 or abs(frame["rotation"]) > 360:
            raise ValueError("Transformação fora dos limites.")
        previous_time = frame["time"]
    with Image.open(image_path) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("Imagem excede 16 megapixels.")
        factor = min(1.0, MAX_EDGE / max(image.size))
        # yuv420p requires even pixel dimensions.
        size = tuple(max(2, int(side * factor) // 2 * 2) for side in image.size)
    width, height = size
    original = _load_rgba(image_path, size)
    original_alpha = original[:, :, 3:4].astype(np.float32) / 255
    flattened = original[:, :, :3] * original_alpha + BACKGROUND * (1 - original_alpha)
    if mode == "subject":
        if foreground_path is None or not foreground_path.is_file():
            raise ValueError("Faça o recorte antes de animar a cabeça/personagem.")
        layer = _load_rgba(foreground_path, size)
        mask = (layer[:, :, 3] > 8).astype(np.uint8) * 255
        if not np.any(mask):
            raise ValueError("O recorte está vazio.")
        if np.any(original[:, :, 3] < 250):
            # A transparent source already has an empty background.
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
    alpha = layer[:, :, 3:4].astype(np.float32) / 255
    premultiplied = np.concatenate((layer[:, :, :3].astype(np.float32) * alpha, alpha), axis=2)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # The suffix explicitly selects MP4, and the completed file replaces output
    # atomically so a reader cannot download a half-written preview.
    partial = output_path.with_name(output_path.name + ".partial.mp4")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(FPS), "-i", "pipe:0", "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
        "-threads", "2", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-b:a", "128k", "-t", str(duration), "-movflags", "+faststart", str(partial),
    ]
    process = None
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            assert process.stdin is not None
            try:
                for number in range(math.ceil(duration * FPS)):
                    frame = _frame_transform(keyframes, number / FPS)
                    matrix = cv2.getRotationMatrix2D(center, frame["rotation"], frame["scale"])
                    matrix[0, 2] += frame["x"] * width
                    matrix[1, 2] += frame["y"] * height
                    warped = cv2.warpAffine(premultiplied, matrix, size, flags=cv2.INTER_LINEAR,
                                            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
                    result = warped[:, :, :3] + background * (1 - warped[:, :, 3:4])
                    process.stdin.write(np.clip(result, 0, 255).astype(np.uint8).tobytes())
            except BrokenPipeError as error:
                raise RuntimeError("FFmpeg interrompeu a codificação do vídeo.") from error
            finally:
                process.stdin.close()
            if process.wait(timeout=120) != 0:
                # FFmpeg error text can contain server paths; keep API messages safe.
                raise RuntimeError("FFmpeg não conseguiu codificar o vídeo.")
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError("O vídeo gerado está vazio.")
        partial.replace(output_path)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        partial.unlink(missing_ok=True)
