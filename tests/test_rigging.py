from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from headbop.imaging import segment_image
from headbop.rigging import build_rig


def _avatar(path: Path, *, transparent: bool = True, translucent: bool = False) -> Image.Image:
    image = Image.new("RGBA", (140, 180), (0, 0, 0, 0) if transparent else "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 15, 100, 75), fill=(240, 155, 50, 255))
    draw.rectangle((63, 65, 77, 95), fill=(240, 155, 50, 255))
    draw.rounded_rectangle((30, 85, 110, 160), radius=15, fill=(35, 80, 200, 255))
    if translucent:
        draw.line((40, 24, 40, 70), fill=(240, 155, 50, 128), width=2)
        draw.line((63, 65, 63, 95), fill=(240, 155, 50, 128), width=2)
    image.save(path)
    return image


def _composite(directory: Path, rig: dict) -> Image.Image:
    layers = [Image.open(directory / rig["layers"][name]).convert("RGBA")
              for name in ("background", "body", "head")]
    return Image.alpha_composite(Image.alpha_composite(layers[0], layers[1]), layers[2])


def test_unknown_alpha_character_stays_single_without_anatomy(tmp_path: Path):
    source, foreground = tmp_path / "input.png", tmp_path / "foreground.png"
    original = _avatar(source)
    segmentation = segment_image(source, foreground)
    rig = build_rig(source, foreground, tmp_path, segmentation)
    assert rig["available"] is True
    assert rig["kind"] == "single"
    assert rig["head_rect"] is None
    assert "manualmente" in rig["warning"]
    assert np.array_equal(np.asarray(original), np.asarray(_composite(tmp_path, rig)))
    assert not np.any(np.asarray(Image.open(tmp_path / "rig-head.png"))[:, :, 3])


@pytest.mark.parametrize("transparent,translucent", [(True, False), (True, True), (False, False)])
def test_manual_head_body_preserves_identity_and_overlaps_neck(tmp_path: Path,
                                                              transparent: bool,
                                                              translucent: bool):
    source, foreground = tmp_path / "input.png", tmp_path / "foreground.png"
    original = _avatar(source, transparent=transparent, translucent=translucent)
    segmentation = segment_image(source, foreground)
    rect = [0.25, 0.05, 0.5, 0.45]
    rig = build_rig(source, foreground, tmp_path, segmentation, head_rect=rect)
    assert rig["available"] is True
    assert rig["kind"] == "head-body"
    assert rig["manual"] is True
    assert rig["head_rect"] == rect
    assert rig["layers"] == {name: f"rig-{name}.png" for name in ("background", "body", "head")}
    for name in rig["layers"].values():
        with Image.open(tmp_path / name) as image:
            assert image.size == original.size
            assert image.mode == "RGBA"
    assert np.array_equal(np.asarray(original), np.asarray(_composite(tmp_path, rig)))
    body = np.asarray(Image.open(tmp_path / "rig-body.png"))[:, :, 3]
    head = np.asarray(Image.open(tmp_path / "rig-head.png"))[:, :, 3]
    assert np.count_nonzero((body > 0) & (head > 0)) >= 20
    assert head[35, 70] == 255 and body[35, 70] == 0
    assert body[120, 70] == 255 and head[120, 70] == 0
    assert 0 < rig["pivots"]["head"][1] < rig["pivots"]["body"][1] < 1


def test_portrait_head_is_single_with_neck_pivot_and_static_background(tmp_path: Path):
    source = Path(__file__).parent / "fixtures" / "astronaut.jpg"
    foreground = tmp_path / "foreground.png"
    segmentation = segment_image(source, foreground)
    rig = build_rig(source, foreground, tmp_path, segmentation)
    assert rig["kind"] == "single"
    x, y, w, h = segmentation["head_rect"]
    assert rig["pivots"]["body"] == pytest.approx([x + w / 2, y + h * 0.88])
    with Image.open(source) as original:
        assert np.array_equal(np.asarray(original.convert("RGBA")),
                              np.asarray(_composite(tmp_path, rig)))
    background = np.asarray(Image.open(tmp_path / "rig-background.png"))
    with Image.open(source) as original:
        assert np.array_equal(background[128, 128, :3], np.asarray(original)[128, 128])


def test_ambiguous_image_uses_whole_fallback_without_assets(tmp_path: Path):
    source = tmp_path / "blank.png"
    Image.new("RGB", (80, 80), "white").save(source)
    foreground = tmp_path / "foreground.png"
    segmentation = segment_image(source, foreground)
    rig = build_rig(source, foreground, tmp_path, segmentation)
    assert rig["available"] is False
    assert rig["kind"] == "whole"
    assert rig["confidence"] == 0
    assert rig["layers"] == {}
    assert "manualmente" in rig["warning"]


def test_manual_rectangle_without_body_falls_back_single(tmp_path: Path):
    source, foreground = tmp_path / "input.png", tmp_path / "foreground.png"
    _avatar(source)
    segmentation = segment_image(source, foreground)
    rig = build_rig(source, foreground, tmp_path, segmentation, head_rect=[0, 0, 1, 1])
    assert rig["kind"] == "single"
    assert "movimento único" in rig["warning"]


@pytest.mark.parametrize("rect", [[-0.1, 0, 0.5, 0.5], [0, 0, 2, 1], [0, 0, 0, 1],
                                  [0, 0, float("nan"), 1], [0, 0, 0.001, 0.001], [0, 0, 1]])
def test_rejects_invalid_manual_head_rectangle(tmp_path: Path, rect):
    source = tmp_path / "input.png"
    _avatar(source)
    with pytest.raises(ValueError):
        build_rig(source, None, tmp_path, {"available": False}, head_rect=rect)


def test_rejects_mismatched_foreground_dimensions(tmp_path: Path):
    source, foreground = tmp_path / "input.png", tmp_path / "foreground.png"
    _avatar(source)
    Image.new("RGBA", (50, 60), "red").save(foreground)
    with pytest.raises(ValueError, match="dimensões"):
        build_rig(source, foreground, tmp_path, {"available": True})


def test_automatic_head_body_requires_face_and_connected_body_metadata(tmp_path: Path):
    source = Path(__file__).parent / "fixtures" / "astronaut.jpg"
    head_path = tmp_path / "head.png"
    segment_image(source, head_path)
    with Image.open(source) as image:
        rgba = np.asarray(image.convert("RGBA")).copy()
    alpha = np.asarray(Image.open(head_path))[:, :, 3].copy()
    alpha[80:245, 75:165] = 255
    rgba[:, :, 3] = alpha
    transparent = tmp_path / "astronaut-transparent.png"
    Image.fromarray(rgba).save(transparent)
    foreground = tmp_path / "foreground.png"
    segmentation = segment_image(transparent, foreground)
    rig = build_rig(transparent, foreground, tmp_path, segmentation)
    assert rig["kind"] == "head-body"
    assert rig["manual"] is False
    assert rig["head_rect"] == segmentation["head_rect"]
    assert np.array_equal(rgba, np.asarray(_composite(tmp_path, rig)))
