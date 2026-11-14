"""Bounded, seed-guided selection with precise additive/eraser refinement.

This is local edge/color segmentation, not semantic recognition. Smart expansion
stays near the user's marks; disconnected components without a positive seed are
removed. Every result retains the source RGB and never increases source alpha.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from .imaging import _GRABCUT_LOCK

MAX_PAINT_EDGE = 640
MAX_SOURCE_EDGE = 1600
MAX_POINTS = 5000


class PaintStroke(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    points: list[tuple[float, float]] = Field(min_length=1, max_length=1000)
    radius: float = Field(ge=0.002, le=0.2)
    mode: Literal["add", "erase"]

    @model_validator(mode="after")
    def normalized_points(self):
        if any(not (0 <= x <= 1 and 0 <= y <= 1) for x, y in self.points):
            raise ValueError("As pinceladas devem ficar dentro da imagem.")
        return self


class PaintSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    strokes: list[PaintStroke] = Field(min_length=1, max_length=100)
    smart: bool = True
    base: Literal["empty", "current"] = "empty"

    @model_validator(mode="after")
    def bounded_points(self):
        if sum(len(stroke.points) for stroke in self.strokes) > MAX_POINTS:
            raise ValueError("Simplifique a seleção: use menos pontos de pincel.")
        return self


def _marks(strokes: list[PaintStroke], width: int, height: int) -> np.ndarray:
    marks = np.zeros((height, width), np.uint8)
    for stroke in strokes:
        radius = max(1, round(stroke.radius * min(width, height)))
        value = 1 if stroke.mode == "add" else 2
        points = [(min(width - 1, round(x * width)), min(height - 1, round(y * height)))
                  for x, y in stroke.points]
        for start, end in zip(points, points[1:]):
            cv2.line(marks, start, end, value, 2 * radius, cv2.LINE_8)
        for point in points:
            cv2.circle(marks, point, radius, value, -1, cv2.LINE_8)
    return marks


def _smart_mask(rgb: np.ndarray, marks: np.ndarray, source_alpha: np.ndarray,
                radius: float) -> np.ndarray:
    positive = (marks == 1) & (source_alpha > 8)
    negative = (marks == 2) | (source_alpha <= 8)
    if not np.any(positive):
        return positive.astype(np.uint8) * 255
    height, width = marks.shape
    # Local support prevents a mark on a face from activating a distant region
    # merely because it has similar colors. Larger parts need more brush marks.
    padding = max(8, round(min(width, height) * min(0.45, max(0.35, 4 * radius))))
    distance = cv2.distanceTransform((~positive).astype(np.uint8), cv2.DIST_L2, 5)
    support = distance <= padding
    classes = np.full((height, width), cv2.GC_BGD, np.uint8)
    classes[support] = cv2.GC_PR_BGD
    classes[positive] = cv2.GC_FGD
    classes[negative] = cv2.GC_BGD
    # GrabCut requires examples of both classes. An intentionally full canvas
    # painting is still valid; no artificial background is invented for it.
    if not np.any(classes == cv2.GC_BGD):
        return positive.astype(np.uint8) * 255
    try:
        with _GRABCUT_LOCK:
            cv2.setRNGSeed(42)
            cv2.grabCut(rgb, classes, None, np.zeros((1, 65), np.float64),
                        np.zeros((1, 65), np.float64), 3, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        # Tiny or low-variation pictures can have insufficient color clusters.
        # Exact brush coverage remains a meaningful deterministic selection.
        return positive.astype(np.uint8) * 255
    candidate = ((classes == cv2.GC_FGD) | (classes == cv2.GC_PR_FGD)) & support & ~negative
    _, components = cv2.connectedComponents(candidate.astype(np.uint8))
    retained = np.unique(components[positive])
    retained = retained[retained != 0]
    selected = np.isin(components, retained) if retained.size else positive
    return selected.astype(np.uint8) * 255


def paint_selection(source: Path, destination: Path, selection: PaintSelection,
                    current: Path | None = None) -> dict:
    """Write a full-size cutout into a caller-owned staging location.

    Replaying the complete stroke history is deterministic with respect to brush
    ordering. ``base=current`` only changes pixels reached by new strokes/smart
    expansion; unpainted existing selection stays unchanged.
    """
    with Image.open(source) as image:
        if min(image.size) < 8 or max(image.size) > MAX_SOURCE_EDGE:
            raise ValueError("A imagem normalizada excede o tamanho permitido para o pincel.")
        rgba = np.asarray(image.convert("RGBA")).copy()
    height, width = rgba.shape[:2]
    marks = _marks(selection.strokes, width, height)
    base_alpha = np.zeros((height, width), np.uint8)
    if selection.base == "current":
        if current is None or not current.is_file():
            raise ValueError("Ainda não há uma área selecionada para refinar.")
        with Image.open(current) as existing:
            if existing.size != (width, height):
                raise ValueError("O recorte e a imagem original devem ter as mesmas dimensões.")
            base_alpha = np.asarray(existing.convert("RGBA"))[:, :, 3].copy()
    selected = (marks == 1).astype(np.uint8) * 255
    if selection.smart and np.any(selected):
        factor = min(1.0, MAX_PAINT_EDGE / max(width, height))
        size = (max(8, round(width * factor)), max(8, round(height * factor)))
        small_marks = _marks(selection.strokes, *size)
        rgb = cv2.resize(rgba[:, :, :3], size, interpolation=cv2.INTER_AREA)
        alpha = cv2.resize(rgba[:, :, 3], size, interpolation=cv2.INTER_AREA)
        # Transparent RGB is hidden data and must not influence segmentation.
        rgb = np.rint(rgb * (alpha[:, :, None] / 255)
                      + 127 * (1 - alpha[:, :, None] / 255)).astype(np.uint8)
        radius = max(stroke.radius for stroke in selection.strokes if stroke.mode == "add")
        smart = _smart_mask(rgb, small_marks, alpha, radius)
        selected = cv2.resize(smart, (width, height), interpolation=cv2.INTER_LINEAR)
        # Full-resolution marks are authoritative even after small-image work.
        selected[marks == 1] = 255
    selected = np.maximum(selected, base_alpha)
    selected[marks == 2] = 0
    rgba[:, :, 3] = np.minimum(selected, rgba[:, :, 3])
    ys, xs = np.nonzero(rgba[:, :, 3] > 8)
    if len(xs) < 9:
        raise ValueError("Pinte uma área visível da imagem antes de aplicar.")
    bounds = [float(xs.min() / width), float(ys.min() / height),
              float((xs.max() - xs.min() + 1) / width),
              float((ys.max() - ys.min() + 1) / height)]
    if not all(math.isfinite(value) for value in bounds):
        raise ValueError("Seleção de pincel inválida.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba).save(destination, format="PNG")
    return {"available": True, "method": "smart-brush" if selection.smart else "manual-brush",
            "rect": bounds, "kind": "unknown", "head_rect": None, "confidence": 1.0,
            "warning": "A área pintada se move como uma camada. O restante fica no fundo; confira as bordas antes de gerar."}
