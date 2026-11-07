"""Conservative local masks, without downloaded models or semantic claims.

Confidence describes evidence for a usable foreground mask, not the probability
of a person or a correct anatomical split. Transparency is authoritative. A
uniform border can establish a color/contrast mask; OpenCV's bundled face
cascade can establish a head region. Other images require a manual rectangle.
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


def _bounds(alpha: np.ndarray) -> list[float]:
    ys, xs = np.nonzero(alpha > 8)
    height, width = alpha.shape
    return [float(xs.min() / width), float(ys.min() / height),
            float((xs.max() - xs.min() + 1) / width),
            float((ys.max() - ys.min() + 1) / height)]


def _faces(rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if detector.empty():
        return []
    faces = detector.detectMultiScale(
        cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), scaleFactor=1.1,
        minNeighbors=5, minSize=(24, 24),
    )
    return [tuple(int(v) for v in face) for face in faces]


def _face_rect(face: tuple[int, int, int, int], width: int, height: int) -> list[float]:
    x, y, w, h = face
    left, top = max(0, int(x - 0.25 * w)), max(0, int(y - 0.4 * h))
    right, bottom = min(width, int(x + 1.25 * w)), min(height, int(y + 1.15 * h))
    return [left / width, top / height, (right - left) / width, (bottom - top) / height]


def _anatomy(alpha: np.ndarray, faces: list[tuple[int, int, int, int]]) -> tuple[str, list | None]:
    """Require one supported face and substantial connected foreground below it."""
    if len(faces) != 1:
        return "unknown", None
    height, width = alpha.shape
    x, y, w, h = faces[0]
    if np.mean(alpha[y:y + h, x:x + w] > 8) < 0.65:
        return "unknown", None
    rect = _face_rect(faces[0], width, height)
    bottom = round((rect[1] + rect[3]) * height)
    mask = (alpha > 8).astype(np.uint8)
    _, labels = cv2.connectedComponents(mask)
    label = labels[min(height - 1, y + h // 2), min(width - 1, x + w // 2)]
    if label == 0:
        return "unknown", None
    connected = labels == label
    low = min(height, bottom + max(1, round(0.2 * h)))
    ys, _ = np.nonzero(connected)
    below = connected[low:, max(0, x - w):min(width, x + 2 * w)]
    has_body = (ys.max() >= bottom + 0.75 * h and np.count_nonzero(below) > 0.5 * w * h
                and np.count_nonzero(below) > 0.18 * np.count_nonzero(connected))
    return ("character" if has_body else "head"), rect


def _uniform_foreground(rgb: np.ndarray) -> tuple[np.ndarray, float] | None:
    """Find contrast against an observed uniform border; never infer anatomy."""
    height, width = rgb.shape[:2]
    border = np.concatenate((rgb[0], rgb[-1], rgb[1:-1, 0], rgb[1:-1, -1])).astype(np.float32)
    color = np.median(border, axis=0)
    border_distance = np.linalg.norm(border - color, axis=1)
    uniformity = float(np.mean(border_distance <= 12))
    if uniformity < 0.90:
        return None
    distance = np.linalg.norm(rgb.astype(np.float32) - color, axis=2)
    near_background = (distance <= 18).astype(np.uint8)
    _, labels = cv2.connectedComponents(near_background)
    exterior = np.unique(np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1])))
    exterior = exterior[exterior != 0]
    mask = ~np.isin(labels, exterior)
    # Label zero is contrast foreground; uniform-colored enclosed details such
    # as white eyes stay opaque because they do not reach the image border.
    count, components, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
    if count <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    total = int(areas.sum())
    coverage = total / (height * width)
    if not 0.015 <= coverage <= 0.85 or areas.max() / max(1, total) < 0.80:
        return None
    # Discard isolated compression speckles, retaining disconnected large details.
    retained = np.flatnonzero(areas >= max(4, total * 0.001)) + 1
    mask = np.isin(components, retained)
    contrast = float(np.median(distance[mask]))
    if contrast < 32:
        return None
    # An explicit, bounded heuristic quality score, not calibrated semantics.
    confidence = min(0.90, 0.65 + 0.15 * uniformity + 0.10 * min(1, contrast / 100))
    return mask.astype(np.uint8) * 255, confidence


def segment_image(source: Path, destination: Path, rect: list[float] | None = None) -> dict:
    """Write a full-canvas RGBA cutout, preserving the original public API.

    Automatic failures return actionable metadata. Manual rectangles are in
    normalized source coordinates. Image processing is bounded to an 800px edge.
    """
    with Image.open(source) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS or min(image.size) < 8:
            raise ValueError("Imagem deve ter pelo menos 8 pixels por lado e até 16 megapixels.")
        rgba = np.asarray(image.convert("RGBA")).copy()
    height, width = rgba.shape[:2]
    alpha = rgba[:, :, 3].copy()
    if not np.any(alpha > 8):
        raise ValueError("A imagem está totalmente transparente.")
    factor = min(1.0, SEGMENT_MAX_EDGE / max(width, height))
    small_width, small_height = max(8, round(width * factor)), max(8, round(height * factor))
    rgb = cv2.resize(rgba[:, :, :3], (small_width, small_height), interpolation=cv2.INTER_AREA)
    small_alpha = cv2.resize(alpha, (small_width, small_height), interpolation=cv2.INTER_AREA)
    if rect is None and np.count_nonzero(alpha < 250) >= max(4, width * height * 0.005):
        # Hidden RGB in fully transparent pixels must not influence face detection.
        matte = (rgb.astype(np.float32) * (small_alpha[:, :, None] / 255)
                 + 127 * (1 - small_alpha[:, :, None] / 255)).astype(np.uint8)
        kind, head_rect = _anatomy(small_alpha, _faces(matte))
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgba).save(destination, format="PNG")
        return {
            "method": "alpha", "available": True, "warning": None,
            "rect": _bounds(alpha), "confidence": 1.0, "kind": kind, "head_rect": head_rect,
        }

    faces = _faces(rgb) if rect is None else []
    if rect is None:
        uniform = _uniform_foreground(rgb)
        if uniform is not None:
            foreground, confidence = uniform
            full_alpha = cv2.resize(foreground, (width, height), interpolation=cv2.INTER_LINEAR)
            rgba[:, :, 3] = np.minimum(alpha, full_alpha)
            kind, head_rect = _anatomy(foreground, faces)
            destination.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(rgba).save(destination, format="PNG")
            return {
                "method": "uniform-background", "available": True,
                "rect": _bounds(rgba[:, :, 3]), "confidence": confidence,
                "kind": kind, "head_rect": head_rect,
                "warning": "Recorte por fundo uniforme, sem identificação anatômica garantida. Confira as bordas; selecione a cabeça para articular.",
            }
        if len(faces) != 1:
            return {
                "method": "unavailable", "available": False, "rect": None,
                "confidence": 0.0, "kind": "unknown", "head_rect": None,
                "warning": "Não foi possível isolar um único rosto ou fundo uniforme. Selecione a cabeça/personagem manualmente ou use um PNG transparente.",
            }
        rect = _face_rect(faces[0], small_width, small_height)
        method = "face-grabcut"
    else:
        method = "manual-grabcut"
    box = _rectangle(rect, small_width, small_height)
    mask = np.zeros((small_height, small_width), np.uint8)
    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)
    try:
        with _GRABCUT_LOCK:
            cv2.setRNGSeed(42)
            cv2.grabCut(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), mask, box,
                        background_model, foreground_model, 5, cv2.GC_INIT_WITH_RECT)
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
        "confidence": 0.75 if method == "face-grabcut" else 0.65,
        "kind": "head" if method == "face-grabcut" else "unknown",
        "head_rect": [float(v) for v in rect] if method == "face-grabcut" else None,
        "warning": "Recorte por contraste: confira as bordas. O preenchimento do fundo no vídeo pode deixar artefatos.",
    }
