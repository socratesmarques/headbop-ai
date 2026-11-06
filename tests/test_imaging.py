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
