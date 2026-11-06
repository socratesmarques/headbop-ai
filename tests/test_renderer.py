import json
import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf
from PIL import Image, ImageDraw

from headbop.renderer import _frame_transform, render_video


def frames(duration=1.0, x=0.0):
    return [{"time": time, "x": x, "y": 0.0, "rotation": 0.0, "scale": 1.0}
            for time in (0.0, duration)]


@pytest.fixture
def media(tmp_path):
    image, audio = tmp_path / "image.png", tmp_path / "audio.wav"
    canvas = Image.new("RGB", (160, 120), "white")
    ImageDraw.Draw(canvas).rectangle((50, 30, 110, 90), fill="red")
    canvas.save(image)
    sf.write(audio, 0.1 * np.sin(2 * np.pi * 440 * np.arange(22050) / 22050), 22050)
    return image, audio


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="FFmpeg required")
def test_render_creates_playable_video_with_audio_and_transformation(media, tmp_path):
    image, audio = media
    output = tmp_path / "video.mp4"
    render_video(image, None, audio, output, frames(x=0.2), 1.0, "whole")
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)
    ]))
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert video["width"] == 160 and video["height"] == 120
    assert video["r_frame_rate"] == "24/1"
    assert int(video["nb_frames"]) == 24
    assert any(stream["codec_type"] == "audio" and stream["codec_name"] == "aac" for stream in metadata["streams"])
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
                                   "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(120, 160, 3)
    assert pixels[60, 5].max() < 40  # exposed neutral canvas on the left
    assert pixels[60, 120, 0] > 200 and pixels[60, 120, 1] < 50  # moved red subject
    assert not output.with_name("video.mp4.partial.mp4").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg required")
def test_subject_mode_removes_original_subject(media, tmp_path):
    image, audio = media
    foreground, output = tmp_path / "cutout.png", tmp_path / "subject.mp4"
    layer = Image.new("RGBA", (160, 120), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rectangle((50, 30, 110, 90), fill=(255, 0, 0, 255))
    layer.save(foreground)
    render_video(image, foreground, audio, output, frames(x=0.25), 1.0, "subject")
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
                                   "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(120, 160, 3)
    assert pixels[60, 60].min() > 230  # old object position is filled, not duplicated
    assert pixels[60, 125, 0] > 200 and pixels[60, 125, 1] < 50


def test_subject_without_cutout_is_rejected(media, tmp_path):
    image, audio = media
    with pytest.raises(ValueError, match="recorte"):
        render_video(image, None, audio, tmp_path / "video.mp4", frames(), 1.0, "subject")


def test_invalid_timeline_rejected_before_render(media, tmp_path):
    image, audio = media
    invalid = frames()
    invalid[1]["time"] = 0
    with pytest.raises(ValueError, match="ordem"):
        render_video(image, None, audio, tmp_path / "video.mp4", invalid, 1.0, "whole")


def test_sparse_keyframes_are_interpolated():
    timeline = frames()
    timeline[-1]["x"] = 0.2
    assert _frame_transform(timeline, 0.5)["x"] == pytest.approx(0.1)
