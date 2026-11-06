"""Local, bounded image segmentation without downloading model weights.

Existing transparency is the most reliable input. Opaque photographs use the
OpenCV bundled frontal-face detector and GrabCut; arbitrary characters require
the user's rectangle. These methods are not semantic person segmentation.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

_GRABCUT_LOCK = threading.Lock()
MAX_IMAGE_PIXELS = 16_000_000
SEGMENT_MAX_EDGE = 800


def _rectangle(rect: list[float], width: int, height: int) -> tuple[int, int, int, int]:
    if len(rect) != 4 or any(not math.isfinite(float(v)) for v in rect):
        raise ValueError("Seleção inválida: informe x, y, largura e altura.")
    x, y, w, h = (float(v) for v in rect)
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
        raise ValueError("A seleção deve estar dentro da imagem.")
    # GrabCut needs known background, so retain a one-pixel exterior border.
    left, top = max(1, int(x * width)), max(1, int(y * height))
    right = min(width - 1, math.ceil((x + w) * width))
    bottom = min(height - 1, math.ceil((y + h) * height))
    if right - left < 3 or bottom - top < 3:
        raise ValueError("Selecione uma região maior para recortar.")
    return left, top, right - left, bottom - top


def segment_image(source: Path, destination: Path, rect: list[float] | None = None) -> dict:
    """Write a full-canvas RGBA cutout and return honest segmentation metadata.

    An unavailable automatic face detection is a normal result, not a guessed
    cutout. Manual rectangles are normalized image coordinates, not screen pixels.
    Processing is downscaled before GrabCut; output retains source dimensions.
    """
    with Image.open(source) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS or min(image.size) < 8:
            raise ValueError("Imagem deve ter pelo menos 8 pixels por lado e até 16 megapixels.")
        rgba = np.asarray(image.convert("RGBA")).copy()
    height, width = rgba.shape[:2]
    alpha = rgba[:, :, 3]
    if not np.any(alpha > 8):
        raise ValueError("A imagem está totalmente transparente.")
    if rect is None and np.count_nonzero(alpha < 250) >= max(4, width * height * 0.005):
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgba).save(destination, format="PNG")
        ys, xs = np.nonzero(alpha > 8)
        return {
            "method": "alpha", "available": True, "warning": None,
            "rect": [float(xs.min() / width), float(ys.min() / height),
                     float((xs.max() - xs.min() + 1) / width),
                     float((ys.max() - ys.min() + 1) / height)],
        }

    factor = min(1.0, SEGMENT_MAX_EDGE / max(width, height))
    small_width, small_height = max(8, round(width * factor)), max(8, round(height * factor))
    rgb = cv2.resize(rgba[:, :, :3], (small_width, small_height), interpolation=cv2.INTER_AREA)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    if rect is None:
        detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if detector.empty():
            raise RuntimeError("Modelo de detecção facial do OpenCV indisponível.")
        faces = detector.detectMultiScale(
            cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), scaleFactor=1.1,
            minNeighbors=5, minSize=(24, 24),
        )
        if len(faces) == 0:
            return {
                "method": "unavailable", "available": False, "rect": None,
                "warning": "Nenhum rosto frontal identificado. Selecione a cabeça/personagem manualmente ou use um PNG transparente.",
            }
        x, y, w, h = max(faces, key=lambda face: int(face[2]) * int(face[3]))
        left = max(0, int(x - 0.25 * w))
        top = max(0, int(y - 0.4 * h))
        right = min(small_width, int(x + 1.25 * w))
        bottom = min(small_height, int(y + 1.15 * h))
        rect = [left / small_width, top / small_height,
                (right - left) / small_width, (bottom - top) / small_height]
        method = "face-grabcut"
    else:
        method = "manual-grabcut"
    box = _rectangle(rect, small_width, small_height)
    mask = np.zeros((small_height, small_width), np.uint8)
    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)
    try:
        # OpenCV's random initializer has shared state. Keep results repeatable.
        with _GRABCUT_LOCK:
            cv2.setRNGSeed(42)
            cv2.grabCut(bgr, mask, box, background_model, foreground_model, 5, cv2.GC_INIT_WITH_RECT)
    except cv2.error as error:
        raise ValueError("Não foi possível separar a seleção do fundo. Ajuste o retângulo ou use PNG transparente.") from error
    foreground = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    if np.count_nonzero(foreground) < 9:
        raise ValueError("A seleção não contém um objeto separável. Inclua o objeto completo e um pouco de fundo.")
    full_alpha = cv2.resize(foreground, (width, height), interpolation=cv2.INTER_LINEAR)
    rgba[:, :, 3] = np.minimum(alpha, full_alpha)
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba).save(destination, format="PNG")
    return {
        "method": method, "available": True, "rect": [float(v) for v in rect],
        "warning": "Recorte por contraste: confira as bordas. O preenchimento do fundo no vídeo pode deixar artefatos.",
    }
