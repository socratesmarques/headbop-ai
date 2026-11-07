from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from headbop.imaging import segment_image


def test_real_nasa_portrait_detects_and_segments_head(tmp_path: Path):
    source = Path(__file__).parent / "fixtures" / "astronaut.jpg"
    output = tmp_path / "portrait-cutout.png"
    result = segment_image(source, output)
    assert result["available"] is True
    assert result["method"] == "face-grabcut"
    alpha = np.asarray(Image.open(output))[:, :, 3]
    assert alpha[55, 110] == 255  # center of the face survives
    assert alpha[128, 128] == 0  # torso does not become the moving head
    assert 1000 < np.count_nonzero(alpha) < 10000
    assert alpha[0, 0] == 0


def test_preserves_existing_transparency(tmp_path: Path):
    image = Image.new("RGBA", (80, 60), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((20, 10, 60, 50), fill=(220, 80, 30, 255))
    source, output = tmp_path / "input.png", tmp_path / "output.png"
    image.save(source)
    result = segment_image(source, output)
    assert result["available"] is True
    assert result["method"] == "alpha"
    assert result["rect"][0] == pytest.approx(0.25)
    assert np.array_equal(np.asarray(image), np.asarray(Image.open(output)))


def test_manual_grabcut_separates_high_contrast_object(tmp_path: Path):
    image = Image.new("RGB", (120, 100), (230, 230, 230))
    ImageDraw.Draw(image).ellipse((35, 20, 85, 80), fill=(30, 100, 210))
    source, output = tmp_path / "input.png", tmp_path / "output.png"
    image.save(source)
    result = segment_image(source, output, [0.2, 0.1, 0.6, 0.8])
    assert result["method"] == "manual-grabcut"
    alpha = np.asarray(Image.open(output))[:, :, 3]
    assert alpha[50, 60] == 255
    assert alpha[5, 5] == 0
    assert 1500 < np.count_nonzero(alpha) < 4000


def test_no_face_returns_actionable_unavailable_result(tmp_path: Path):
    source, output = tmp_path / "input.png", tmp_path / "output.png"
    Image.new("RGB", (100, 100), "white").save(source)
    result = segment_image(source, output)
    assert result["available"] is False
    assert "manualmente" in result["warning"]
    assert not output.exists()


@pytest.mark.parametrize("rect", [[-0.1, 0, 0.5, 0.5], [0, 0, 2, 1], [0, 0, 0, 1],
                                  [0, 0, float("nan"), 1], [0, 0, 0.001, 0.001]])
def test_rejects_bad_selection(tmp_path: Path, rect):
    source = tmp_path / "input.png"
    Image.new("RGB", (100, 100), "white").save(source)
    with pytest.raises(ValueError):
        segment_image(source, tmp_path / "output.png", rect)


def test_rejects_invisible_input(tmp_path: Path):
    source = tmp_path / "input.png"
    Image.new("RGBA", (100, 100), (10, 20, 30, 0)).save(source)
    with pytest.raises(ValueError, match="transparente"):
        segment_image(source, tmp_path / "output.png")


def test_uniform_background_avatar_preserves_enclosed_white_details(tmp_path: Path):
    image = Image.new("RGB", (140, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 15, 100, 75), fill=(240, 155, 50))
    draw.rectangle((63, 65, 77, 95), fill=(240, 155, 50))
    draw.rounded_rectangle((30, 85, 110, 160), radius=15, fill=(35, 80, 200))
    draw.ellipse((50, 32, 61, 44), fill="white")
    draw.ellipse((79, 32, 90, 44), fill="white")
    source, output = tmp_path / "avatar.png", tmp_path / "cutout.png"
    image.save(source)
    result = segment_image(source, output)
    alpha = np.asarray(Image.open(output))[:, :, 3]
    assert result["available"] is True
    assert result["method"] == "uniform-background"
    assert 0.65 <= result["confidence"] <= 0.9
    assert result["kind"] == "unknown"  # A silhouette is not anatomical evidence.
    assert result["head_rect"] is None
    assert alpha[35, 55] == 255  # White eye is part of the character.
    assert alpha[120, 70] == 255
    assert alpha[0, 0] == 0
    assert "fundo uniforme" in result["warning"]


def test_uniform_background_does_not_choose_between_two_objects(tmp_path: Path):
    image = Image.new("RGB", (160, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((15, 20, 65, 80), fill=(30, 100, 200))
    draw.ellipse((95, 20, 145, 80), fill=(30, 100, 200))
    source, output = tmp_path / "ambiguous.png", tmp_path / "cutout.png"
    image.save(source)
    result = segment_image(source, output)
    assert result["available"] is False
    assert result["confidence"] == 0
    assert result["kind"] == "unknown"
    assert result["head_rect"] is None
    assert not output.exists()


def test_textured_scene_returns_manual_fallback(tmp_path: Path):
    yy, xx = np.indices((120, 120))
    pattern = ((xx // 10 + yy // 10) % 2).astype(np.uint8)
    rgb = np.stack((pattern * 190, pattern * 60, 255 - pattern * 120), axis=2)
    source = tmp_path / "scene.png"
    Image.fromarray(rgb).save(source)
    result = segment_image(source, tmp_path / "foreground.png")
    assert result["available"] is False
    assert "manualmente" in result["warning"]


def test_transparency_quality_does_not_claim_anatomy(tmp_path: Path):
    image = Image.new("RGBA", (80, 60))
    ImageDraw.Draw(image).ellipse((20, 10, 60, 50), fill=(40, 60, 210, 128))
    source, output = tmp_path / "translucent.png", tmp_path / "cutout.png"
    image.save(source)
    result = segment_image(source, output)
    assert result["confidence"] == 1.0
    assert result["kind"] == "unknown"
    assert result["head_rect"] is None
    assert np.array_equal(np.asarray(image), np.asarray(Image.open(output)))


def test_real_face_with_connected_body_enables_anatomical_hint(tmp_path: Path):
    source = Path(__file__).parent / "fixtures" / "astronaut.jpg"
    head_path = tmp_path / "head.png"
    segment_image(source, head_path)
    with Image.open(source) as image:
        rgba = np.asarray(image.convert("RGBA")).copy()
    alpha = np.asarray(Image.open(head_path))[:, :, 3].copy()
    # Keep the actual photograph's face and torso; this fixture's artificial
    # transparent silhouette tests whether body evidence accompanies that face.
    alpha[80:245, 75:165] = 255
    rgba[:, :, 3] = alpha
    transparent = tmp_path / "astronaut-transparent.png"
    Image.fromarray(rgba).save(transparent)
    result = segment_image(transparent, tmp_path / "full-cutout.png")
    assert result["method"] == "alpha"
    assert result["kind"] == "character"
    assert result["head_rect"] is not None
