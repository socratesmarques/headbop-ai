"""Full-canvas head/body layers with explicit evidence and safe fallbacks.

A foreground mask alone establishes an object, not anatomy. Automatic splitting
requires a supported face plus connected body evidence from imaging; an explicit
head rectangle can establish the split on any existing foreground. Pivots and
rectangles use normalized coordinates of the original, uncropped image.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .imaging import MAX_IMAGE_PIXELS

_LAYER_NAMES = {name: f"rig-{name}.png" for name in ("background", "body", "head")}


def _validated_rectangle(rect: list[float], width: int, height: int) -> list[float]:
    try:
        values = [float(value) for value in rect]
    except (TypeError, ValueError) as error:
        raise ValueError("Cabeça inválida: informe x, y, largura e altura normalizados.") from error
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("Cabeça inválida: informe x, y, largura e altura normalizados.")
    x, y, w, h = values
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
        raise ValueError("O retângulo da cabeça deve ficar dentro da imagem.")
    if w * width < 3 or h * height < 3:
        raise ValueError("Selecione uma cabeça com pelo menos três pixels por lado.")
    return values


def _background(source: np.ndarray, foreground: np.ndarray) -> np.ndarray:
    """Keep known pixels, inpaint fully covered opaque pixels, preserve alpha.

    Partial-alpha source edges remain source-colored. The residual background
    alpha solves foreground-over-background = source, so identity composition
    does not darken antialiasing or double the opacity of a transparent PNG.
    """
    background = source.copy()
    source_alpha = source[:, :, 3].astype(np.float32) / 255
    foreground_alpha = foreground[:, :, 3].astype(np.float32) / 255
    denominator = 1 - foreground_alpha
    residual = np.divide(source_alpha - foreground_alpha, denominator,
                         out=np.zeros_like(source_alpha), where=denominator > 0)
    opaque = foreground[:, :, 3] == 255
    # For an opaque photograph the removed area has an opaque reconstructed
    # backdrop. For alpha artwork it remains transparent, without invented sky.
    transparent_source = np.any(source[:, :, 3] < 250)
    if not transparent_source and np.any(opaque):
        # Bound inpainting work independently of the uploaded image size.
        height, width = opaque.shape
        factor = min(1.0, 800 / max(width, height))
        size = (max(1, round(width * factor)), max(1, round(height * factor)))
        small_rgb = cv2.resize(source[:, :, :3], size, interpolation=cv2.INTER_AREA)
        small_mask = cv2.resize(opaque.astype(np.uint8) * 255, size,
                                interpolation=cv2.INTER_NEAREST)
        if np.any(small_mask == 0):
            filled = cv2.inpaint(small_rgb, small_mask, 3, cv2.INPAINT_TELEA)
            full = cv2.resize(filled, (width, height), interpolation=cv2.INTER_LINEAR)
            background[opaque, :3] = full[opaque]
        residual[opaque] = 1
    background[:, :, 3] = np.rint(np.clip(residual, 0, 1) * 255).astype(np.uint8)
    return background


def _split(foreground: np.ndarray, rect: list[float]) -> tuple[np.ndarray, np.ndarray, int]:
    height, width = foreground.shape[:2]
    x, y, w, h = rect
    left, top = max(0, math.floor(x * width)), max(0, math.floor(y * height))
    right, bottom = min(width, math.ceil((x + w) * width)), min(height, math.ceil((y + h) * height))
    # Extend the visible body row beneath the head into the overlap. Keeping a
    # second copy of chin pixels in the torso produces a detached chin/halo when
    # the head rises. This is a small local extension, not inferred hidden anatomy.
    overlap = max(2, min(16, round((bottom - top) * 0.15)))
    overlap = min(overlap, bottom - top - 1)
    weights = np.zeros((height, width), np.uint8)
    weights[top:bottom, left:right] = 255
    original_alpha = foreground[:, :, 3]
    head = foreground.copy()
    body = foreground.copy()
    head[:, :, 3] = np.where(weights == 255, original_alpha, 0).astype(np.uint8)
    body_alpha = np.where(weights == 255, 0, original_alpha).copy()
    reference = min(height - 1, bottom)
    for row in range(bottom - overlap, bottom):
        # Only opaque head pixels can cover a body extension without changing
        # the identity composite. Semitransparent edge pixels have one owner.
        opaque = original_alpha[row, left:right] == 255
        extension = np.minimum(original_alpha[row, left:right], original_alpha[reference, left:right])
        body_alpha[row, left:right] = np.where(opaque, extension, 0)
        body[row, left:right, :3] = foreground[reference, left:right, :3]
    body[:, :, 3] = body_alpha.astype(np.uint8)
    return body, head, overlap


def build_rig(image_path: Path, foreground_path: Path | None, output_dir: Path,
              segmentation: dict, head_rect: list[float] | None = None) -> dict:
    """Save fixed-name RGBA layers and return a serializable rig description.

    ``whole`` means no trustworthy foreground; ``single`` moves one complete
    cutout, including a detected portrait head; ``head-body`` articulates a
    complete foreground with a supported or user-selected head. The renderer
    composes body @ head, then head over body over the static background.
    """
    image_path, output_dir = Path(image_path), Path(output_dir)
    with Image.open(image_path) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS or min(image.size) < 8:
            raise ValueError("Imagem deve ter pelo menos 8 pixels por lado e até 16 megapixels.")
        source = np.asarray(image.convert("RGBA")).copy()
    height, width = source.shape[:2]
    manual = head_rect is not None
    if manual:
        head_rect = _validated_rectangle(head_rect, width, height)
    fallback = {
        "available": False, "kind": "whole", "confidence": 0.0,
        "warning": "Sem recorte confiável. Anime a imagem inteira ou selecione o personagem manualmente.",
        "head_rect": head_rect, "pivots": {"body": [0.5, 0.5], "head": [0.5, 0.5]},
        "layers": {},
    }
    if not segmentation.get("available") or foreground_path is None or not Path(foreground_path).is_file():
        return fallback
    with Image.open(foreground_path) as image:
        if image.size != (width, height):
            raise ValueError("O recorte e a imagem original devem ter as mesmas dimensões.")
        foreground = np.asarray(image.convert("RGBA")).copy()
    # Only the alpha mask is authoritative. Preserve source colors, including
    # semitransparent edge colors, rather than importing a foreign image layer.
    foreground[:, :, :3] = source[:, :, :3]
    foreground[:, :, 3] = np.minimum(foreground[:, :, 3], source[:, :, 3])
    ys, xs = np.nonzero(foreground[:, :, 3] > 8)
    if len(xs) < 9:
        return fallback
    confidence = float(segmentation.get("confidence", 0.5))
    confidence = min(1.0, max(0.0, confidence)) if math.isfinite(confidence) else 0.0
    body_pivot = [float((xs.min() + xs.max() + 1) / (2 * width)),
                  float((ys.min() + 0.70 * (ys.max() - ys.min() + 1)) / height)]
    head_pivot = body_pivot.copy()
    warning = segmentation.get("warning")
    if head_rect is None and segmentation.get("kind") == "character":
        candidate = segmentation.get("head_rect")
        if candidate is not None:
            head_rect = _validated_rectangle(candidate, width, height)
    kind = "single"
    body = foreground.copy()
    head = np.zeros_like(foreground)
    overlap = 0
    if head_rect is not None:
        split_body, split_head, seam = _split(foreground, head_rect)
        head_pixels = np.count_nonzero(split_head[:, :, 3] > 8)
        body_pixels = np.count_nonzero(split_body[:, :, 3] > 8)
        x, y, w, h = head_rect
        bottom = min(height, math.ceil((y + h) * height))
        below = np.count_nonzero(foreground[bottom:, :, 3] > 8)
        # Body pixels must actually extend beyond the head; the overlap alone
        # is not evidence for an independently moving torso.
        if head_pixels >= 9 and body_pixels >= 9 and below >= max(9, len(xs) * 0.05):
            kind, body, head, overlap = "head-body", split_body, split_head, seam
            head_pivot = [x + w / 2, y + h - seam / (2 * height)]
        elif manual:
            warning = "A seleção não separa cabeça e corpo no recorte. Mantido movimento único; ajuste a seleção ou recorte o personagem completo."
        else:
            head_rect = None
    elif segmentation.get("kind") == "head":
        candidate = segmentation.get("head_rect") or segmentation.get("rect")
        if candidate:
            x, y, w, h = _validated_rectangle(candidate, width, height)
            body_pivot = [x + w / 2, min(1.0, y + h * 0.88)]
            head_pivot = body_pivot.copy()
    elif warning is None:
        warning = "Recorte preservado como uma camada. Selecione a cabeça manualmente para articular cabeça e corpo."
    background = _background(source, foreground)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, layer in (("background", background), ("body", body), ("head", head)):
        Image.fromarray(layer).save(output_dir / _LAYER_NAMES[name], format="PNG")
    return {
        "available": True, "kind": kind, "confidence": confidence, "warning": warning,
        "head_rect": head_rect, "pivots": {"body": body_pivot, "head": head_pivot},
        "layers": _LAYER_NAMES.copy(), "manual": manual, "overlap_px": overlap,
    }
