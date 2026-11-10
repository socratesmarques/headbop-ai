import json
import os
import shutil
import subprocess
import sys
import time

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


@pytest.mark.parametrize("fps,resolution,aspect,dimensions", [
    (24, 640, "source", (640, 480)),
    (30, 720, "square", (720, 720)),
    (60, 1080, "vertical", (1080, 1920)),
    (24, 720, "vertical", (720, 1280)),
    (30, 1080, "source", (1440, 1080)),
])
@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="FFmpeg required")
def test_v2_actual_codec_fps_dimensions_audio(media, tmp_path, fps, resolution, aspect, dimensions):
    image, audio = media
    output = tmp_path / "v2.mp4"
    render_video(image, None, audio, output, frames(duration=0.5), 0.5, "whole",
                 settings={"fps": fps, "resolution": resolution, "aspect": aspect})
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)
    ]))
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    audio_stream = next(stream for stream in metadata["streams"] if stream["codec_type"] == "audio")
    assert (video["width"], video["height"]) == dimensions
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert video["r_frame_rate"] == video["avg_frame_rate"] == f"{fps}/1"
    assert int(video["nb_frames"]) == fps // 2
    assert float(video["duration"]) == pytest.approx(0.5, abs=1 / fps)
    assert audio_stream["codec_name"] == "aac"
    assert float(audio_stream["duration"]) == pytest.approx(0.5, abs=0.05)
    decoded_audio = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(output), "-vn", "-f", "f32le", "-ac", "1", "pipe:1"
    ])
    assert np.sqrt(np.mean(np.frombuffer(decoded_audio, dtype=np.float32) ** 2)) > 0.04


def identity(**changes):
    return {"x": 0.0, "y": 0.0, "rotation": 0.0, "scale": 1.0,
            "scale_x": 1.0, "scale_y": 1.0, **changes}


@pytest.fixture
def split_scene(tmp_path):
    from headbop.renderer import _prepare_scene

    size = (640, 480)
    background = Image.new("RGBA", size, "white")
    body = Image.new("RGBA", size)
    ImageDraw.Draw(body).rectangle((180, 230, 220, 270), fill=(0, 255, 0, 255))
    head = Image.new("RGBA", size)
    ImageDraw.Draw(head).rectangle((198, 108, 202, 112), fill=(255, 0, 0, 255))
    original = Image.alpha_composite(Image.alpha_composite(background, body), head)
    image = tmp_path / "image.png"
    original.save(image)
    for name, layer in (("background", background), ("body", body), ("head", head)):
        layer.save(tmp_path / f"rig-{name}.png")
    rig = {"available": True, "kind": "head-body",
           "pivots": {"body": [0.5, 0.5], "head": [200 / 640, 150 / 480]},
           "layers": {name: f"rig-{name}.png" for name in ("background", "body", "head")}}
    return _prepare_scene(image, None, "subject", {}, rig), np.asarray(original)[:, :, :3], image, rig


def test_zero_motion_reconstructs_split_source_exactly(split_scene):
    from headbop.renderer import _render_frame

    scene, source, _, _ = split_scene
    result = _render_frame(scene, {**identity(), "layers": {"body": identity(), "head": identity()}})
    np.testing.assert_array_equal(result, source)


def test_head_rotation_is_independent_and_pivot_stays_attached_to_parent(split_scene):
    from headbop.renderer import _render_frame

    scene, _, _, _ = split_scene
    result = _render_frame(scene, {**identity(x=0.5), "layers": {
        "body": identity(x=0.1), "head": identity(rotation=90)}})
    # Head at (200,110) rotates around (200,150) to (160,150), then follows body +64px.
    np.testing.assert_array_equal(result[150, 224], [255, 0, 0])
    np.testing.assert_array_equal(result[250, 264], [0, 255, 0])
    np.testing.assert_array_equal(result[110, 200], [255, 255, 255])


def test_head_transform_is_composed_after_parent_rotation(split_scene):
    from headbop.renderer import _render_frame

    scene, _, _, _ = split_scene
    result = _render_frame(scene, {**identity(), "layers": {
        "body": identity(rotation=90), "head": identity(rotation=90)}})
    # Body pivot=(320,240), head pivot=(200,150). Applying head then body gives (230,400).
    np.testing.assert_array_equal(result[400, 230], [255, 0, 0])
    np.testing.assert_array_equal(result[360, 330], [0, 255, 0])
    np.testing.assert_array_equal(result[150, 160], [255, 255, 255])


def test_nonuniform_scale_uses_local_axes_and_keeps_head_pivot(split_scene):
    from headbop.renderer import _render_frame

    scene, _, _, _ = split_scene
    result = _render_frame(scene, {**identity(), "layers": {
        "body": identity(), "head": identity(scale_x=2.0, scale_y=0.5)}})
    red = (result[:, :, 0] > 240) & (result[:, :, 1] < 20)
    ys, xs = np.nonzero(red)
    assert xs.mean() == pytest.approx(200)
    assert ys.mean() == pytest.approx(130)  # 40px pivot distance contracts to 20px
    assert xs.max() - xs.min() > ys.max() - ys.min()
    np.testing.assert_array_equal(result[250, 200], [0, 255, 0])


@pytest.mark.parametrize("aspect", ["square", "vertical"])
def test_fit_preserves_all_four_corners_without_cropping(tmp_path, aspect):
    from headbop.renderer import BACKGROUND, _prepare_scene, _render_frame

    image = tmp_path / "corners.png"
    canvas = Image.new("RGB", (640, 320), "white")
    draw = ImageDraw.Draw(canvas)
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255)]
    for box, color in zip([(0, 0, 40, 40), (599, 0, 639, 40),
                           (0, 279, 40, 319), (599, 279, 639, 319)], colors):
        draw.rectangle(box, fill=color)
    canvas.save(image)
    scene = _prepare_scene(image, None, "whole", {"aspect": aspect})
    pixels = _render_frame(scene, identity())
    width, height = scene.content_size
    left, top = scene.offset
    for (x, y), color in zip([(left + 5, top + 5), (left + width - 6, top + 5),
                              (left + 5, top + height - 6), (left + width - 6, top + height - 6)], colors):
        np.testing.assert_array_equal(pixels[y, x], color)
    np.testing.assert_array_equal(pixels[0, 0], BACKGROUND)


def test_premultiplied_alpha_avoids_dark_or_hidden_color_fringes(tmp_path):
    from headbop.renderer import _prepare_scene, _render_frame

    # Transparent hidden green must not bleed into the red edge during a half-pixel shift.
    rgba = np.zeros((480, 640, 4), dtype=np.uint8)
    rgba[:, :, 1] = 255
    rgba[100:200, 100:200] = [255, 0, 0, 255]
    image = tmp_path / "alpha.png"
    Image.fromarray(rgba).save(image)
    scene = _prepare_scene(image, None, "whole", {})
    result = _render_frame(scene, identity(x=0.5 / 640))
    np.testing.assert_allclose(result[150, 100], [136, 10, 13], atol=1)
    np.testing.assert_array_equal(result[150, 101], [255, 0, 0])


def test_layer_interpolation_includes_scale_axes():
    timeline = [{"time": 0.0, **identity(), "layers": {"body": identity(), "head": identity()}},
                {"time": 1.0, **identity(scale_y=1.5), "layers": {
                    "body": identity(x=0.2), "head": identity(rotation=30, scale_x=1.5)}}]
    sample = _frame_transform(timeline, 0.5)
    assert sample["scale_y"] == pytest.approx(1.25)
    assert sample["layers"]["body"]["x"] == pytest.approx(0.1)
    assert sample["layers"]["head"]["rotation"] == pytest.approx(15)
    assert sample["layers"]["head"]["scale_x"] == pytest.approx(1.25)


@pytest.mark.parametrize("changes", [
    {"scale_x": float("nan")}, {"scale_y": 0.0}, {"x": "0"}, {"rotation": True},
    {"layers": {"body": identity(), "head": identity(y=float("inf"))}},
    {"layers": {"head": identity()}},
])
def test_invalid_transform_samples_rejected_before_ffmpeg(media, tmp_path, monkeypatch, changes):
    def unexpected_encoder(*args, **kwargs):
        pytest.fail("invalid frames must fail before opening encoder")

    monkeypatch.setattr(subprocess, "Popen", unexpected_encoder)
    image, audio = media
    timeline = frames()
    timeline[1].update(changes)
    with pytest.raises(ValueError):
        render_video(image, None, audio, tmp_path / "bad.mp4", timeline, 1.0, "whole", settings={})


@pytest.mark.parametrize("settings", [{"fps": 120}, {"fps": True}, {"resolution": 4096},
                                     {"aspect": "crop"}, {"engine": "unknown"}])
def test_invalid_render_options_are_rejected(media, tmp_path, settings):
    image, audio = media
    with pytest.raises(ValueError):
        render_video(image, None, audio, tmp_path / "bad.mp4", frames(), 1.0, "whole", settings=settings)


def test_thirty_seconds_at_sixty_fps_allows_endpoint_but_bounds_samples():
    from headbop.renderer import _validate_timeline

    timeline = [{"time": index / 60, **identity()} for index in range(1801)]
    _validate_timeline(timeline, 30, 60, False)
    with pytest.raises(ValueError, match="tempo"):
        _validate_timeline(timeline + [{"time": 30, **identity()}], 30, 60, False)


def test_rig_metadata_cannot_select_external_assets(split_scene, tmp_path):
    from headbop.renderer import _prepare_scene, _render_frame

    _, source, image, rig = split_scene
    rig["layers"] = {name: "/does/not/exist.png" for name in ("background", "body", "head")}
    scene = _prepare_scene(image, None, "subject", {}, rig)
    np.testing.assert_array_equal(_render_frame(scene, identity()), source)


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable/process-group regression")
@pytest.mark.parametrize("consume_input", [False, True])
def test_encoder_timeout_covers_blocked_writes_and_final_mux(
        media, tmp_path, monkeypatch, consume_input):
    """The old timeout was unreachable while FFmpeg stopped consuming stdin."""
    encoder = tmp_path / "ffmpeg"
    encoder.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[-1]).write_bytes(b'partial')\n"
        + ("sys.stdin.buffer.read()\n" if consume_input else "")
        + "time.sleep(60)\n")
    encoder.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    original_popen = subprocess.Popen
    encoders = []

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        encoders.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    output = tmp_path / "video.mp4"
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        render_video(media[0], None, media[1], output, frames(), 1.0, "whole",
                     timeout_seconds=0.5)
    assert time.monotonic() - started < 3
    assert len(encoders) == 1 and encoders[0].returncode is not None
    assert encoders[0].stdin.closed
    assert not output.exists()
    assert not output.with_name("video.mp4.partial.mp4").exists()


def test_callback_failure_reaps_encoder_and_removes_partial(media, tmp_path, monkeypatch):
    original_popen = subprocess.Popen
    encoders = []

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        encoders.append(process)
        return process

    def failed_callback(done, total):
        raise RuntimeError("callback failed")

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    output = tmp_path / "video.mp4"
    with pytest.raises(RuntimeError, match="callback failed"):
        render_video(media[0], None, media[1], output, frames(), 1.0, "whole",
                     progress_callback=failed_callback)
    assert encoders[0].returncode is not None and encoders[0].stdin.closed
    assert not output.exists()
    assert not output.with_name("video.mp4.partial.mp4").exists()
