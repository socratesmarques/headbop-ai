import io
import wave
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from headbop import formats
from headbop.settings import Settings


@pytest.fixture
def portable_project():
    image = io.BytesIO()
    Image.new("RGBA", (32, 32), (255, 50, 0, 255)).save(image, format="PNG")
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\0\0" * 22050)
    project = {
        "id": "not-exported",
        "private_path": "/private/server/path",
        "duration": 1.0,
        "settings": {"mode": "subject", "motion": "hard", "bpm": 120.0, "intensity": 0.8},
        "segmentation": {"rect": [0.1, 0.2, 0.4, 0.5]},
        "keyframes": [
            {"time": 0, "x": 0, "y": -0.01, "rotation": 2.0, "scale": 1.05},
            {"time": 1, "x": 0, "y": 0, "rotation": 0, "scale": 1},
        ],
    }
    return project, image.getvalue(), audio.getvalue()


def test_round_trip_real_png_and_wav(portable_project):
    project, image, audio = portable_project
    encoded = formats.export_project(project, image, audio)
    assert encoded.startswith(b"<?xml")
    assert b"/private/server/path" not in encoded
    assert b"not-exported" not in encoded
    imported = formats.import_project(encoded)
    assert imported == {
        "schema_version": 1, "duration": project["duration"],
        "keyframes": project["keyframes"], "settings": project["settings"],
        "image": image, "audio": audio, "rect": project["segmentation"]["rect"],
    }
    assert Image.open(io.BytesIO(imported["image"])).size == (32, 32)
    with wave.open(io.BytesIO(imported["audio"])) as wav:
        assert wav.getnframes() == 22050
    root = ET.fromstring(encoded)
    assert len(root.find("timeline")) == 2


@pytest.mark.parametrize("payload", [
    b"<scene/>", b"<headbop-project version='3'/>", b"<project/>",
])
def test_foreign_and_unknown_versions_are_explicitly_rejected(payload):
    with pytest.raises(ValueError, match="Headbop v1 ou v2"):
        formats.import_project(payload)


@pytest.mark.parametrize("payload", [
    b"<!DOCTYPE x [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><x>&x;</x>",
    b"<!DOCTYPE x [<!ENTITY x 'expand'>]><x>&x;</x>", b"<broken>",
])
def test_dtd_entities_and_malformed_xml_rejected(payload):
    with pytest.raises(ValueError, match="XML inválido ou inseguro"):
        formats.import_project(payload)


@pytest.mark.parametrize("attribute,value", [
    ("bpm", "nan"), ("bpm", "241"), ("bpm", "39"), ("intensity", "inf"),
    ("intensity", "-1"), ("motion", "unknown"), ("mode", "exec"),
])
def test_invalid_settings_rejected(portable_project, attribute, value):
    root = ET.fromstring(formats.export_project(*portable_project))
    root.find("settings").set(attribute, value)
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "nested", "missing", "base64", "mime",
                                         "time", "scale", "rect", "empty_frames", "fps"])
def test_invalid_structure_and_payload_rejected(portable_project, mutation):
    root = ET.fromstring(formats.export_project(*portable_project))
    if mutation == "duplicate":
        root.append(root.find("settings"))
    elif mutation == "extra":
        ET.SubElement(root, "command").text = "do not execute"
    elif mutation == "nested":
        ET.SubElement(root.find("settings"), "unknown")
    elif mutation == "missing":
        root.remove(root.find("assets"))
    elif mutation == "base64":
        root.find("assets/image").text = "invalid$$$"
    elif mutation == "mime":
        root.find("assets/image").set("mime", "text/html")
    elif mutation == "time":
        root.find("timeline/keyframe").set("time", "2")
    elif mutation == "scale":
        root.find("timeline/keyframe").set("scale", "nan")
    elif mutation == "rect":
        root.find("segmentation").set("width", "1")
    elif mutation == "empty_frames":
        for frame in list(root.find("timeline")):
            root.find("timeline").remove(frame)
    elif mutation == "fps":
        root.find("timeline").set("fps", "120")
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))


def test_asset_type_and_size_limits(portable_project, monkeypatch):
    project, image, audio = portable_project
    with pytest.raises(ValueError, match="PNG"):
        formats.export_project(project, b"not a PNG", audio)
    monkeypatch.setattr(formats, "MAX_ASSET_BYTES", 10)
    with pytest.raises(ValueError, match="20 MiB"):
        formats.export_project(project, image, audio)


def test_xml_size_limit_before_parsing(monkeypatch):
    monkeypatch.setattr(formats, "MAX_XML_BYTES", 10)
    with pytest.raises(ValueError, match="56 MiB"):
        formats.import_project(b"x" * 11)


def test_no_recrop_metadata_for_alpha_image(portable_project):
    project, image, audio = portable_project
    project["segmentation"] = {"method": "alpha", "rect": [0.1, 0.2, 0.4, 0.5]}
    imported = formats.import_project(formats.export_project(project, image, audio))
    assert "rect" not in imported
    assert imported["image"] == image


def test_original_v1_fixture_reexports_byte_for_byte():
    # Captured with the original v1 exporter before introducing the v2 schema.
    encoded = (Path(__file__).parent / "fixtures" / "headbop-v1.xml").read_bytes()
    imported = formats.import_project(encoded)
    assert imported["schema_version"] == 1
    assert imported["duration"] == 0.5
    assert imported["settings"] == {
        "mode": "whole", "motion": "smooth", "bpm": 120.0, "intensity": 1.0
    }
    assert imported["keyframes"] == [
        {"time": 0.0, "x": 0.0, "y": 0.01, "rotation": -2.0, "scale": 1.05},
        {"time": 0.5, "x": 0.0, "y": 0.0, "rotation": 0.0, "scale": 1.0},
    ]
    assert formats.export_project(imported, imported["image"], imported["audio"]) == encoded
    assert Image.open(io.BytesIO(imported["image"])).size == (1, 1)
    with wave.open(io.BytesIO(imported["audio"])) as wav:
        assert wav.getnframes() / wav.getframerate() == 0.5


@pytest.fixture
def portable_v2_project(portable_project):
    original, image, audio = portable_project
    project = deepcopy(original)
    project["settings"] = Settings.model_validate({
        "bpm": 120, "mode": "subject", "motion": "auto", "fps": 60,
        "resolution": 1080, "aspect": "vertical", "speed": 1.5,
        "easing": "ease-in-out", "squash": 0.6,
    }).model_dump()
    project["rig"] = {
        "head_rect": [0.2, 0.1, 0.4, 0.3],
        "layers": {"body": "/private/rig-body.png", "head": "/private/rig-head.png"},
    }
    for frame in project["keyframes"]:
        frame.update({"scale_x": 0.98, "scale_y": 1.02})
        frame["layers"] = {
            "body": {key: value for key, value in frame.items() if key != "time"},
            "head": {
                "x": -0.02, "y": 0.03, "rotation": -4.0, "scale": 1.0,
                "scale_x": 1.01, "scale_y": 0.99,
            },
        }
    return project, image, audio


def test_v2_round_trip_preserves_settings_frames_layers_and_head_rect(portable_v2_project):
    project, image, audio = portable_v2_project
    encoded = formats.export_project(project, image, audio)
    root = ET.fromstring(encoded)
    assert root.attrib == {"version": "2"}
    assert root.find("timeline").attrib["fps"] == "60"
    assert {layer.attrib["name"] for layer in root.find("timeline/keyframe")} == {"body", "head"}
    assert b"/private/" not in encoded
    assert b"{'body':" not in encoded
    assert formats.import_project(encoded) == {
        "schema_version": 2, "duration": project["duration"],
        "settings": project["settings"], "keyframes": project["keyframes"],
        "rect": project["segmentation"]["rect"], "head_rect": project["rig"]["head_rect"],
        "image": image, "audio": audio,
    }


def test_legacy_engine_can_round_trip_original_sampled_frames_in_v2(portable_project):
    project, image, audio = portable_project
    project["settings"] = Settings.model_validate({
        **project["settings"], "engine": "legacy"
    }).model_dump()
    encoded = formats.export_project(project, image, audio)
    assert ET.fromstring(encoded).attrib["version"] == "2"
    imported = formats.import_project(encoded)
    assert imported["settings"] == project["settings"]
    assert imported["keyframes"] == project["keyframes"]


@pytest.mark.parametrize("attribute,value", [
    ("fps", "25"), ("fps", "nan"), ("resolution", "800"), ("resolution", "720.1"),
    ("speed", "0.49"), ("speed", "2.01"), ("speed", "inf"),
    ("squash", "-0.01"), ("squash", "1.01"), ("squash", "nan"),
    ("engine", "unknown"), ("engine", "legacy"), ("easing", "unknown"),
    ("aspect", "unknown"), ("motion", "reference"),
])
def test_v2_invalid_settings_rejected(portable_v2_project, attribute, value):
    root = ET.fromstring(formats.export_project(*portable_v2_project))
    root.find("settings").set(attribute, value)
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))


@pytest.mark.parametrize("mutation", [
    "missing_setting", "extra_setting", "timeline_fps", "missing_layer", "duplicate_layer",
    "unknown_layer", "unknown_layer_attribute", "nested_layer", "layer_time", "layer_text",
    "layer_tail", "layer_scale", "layer_rotation", "frame_scale_x", "missing_scale_y",
    "missing_layers", "layer_json", "unknown_frame_element", "rig_rect", "rig_duplicate",
    "rig_missing", "rig_path", "time_order", "duration", "root_text",
])
def test_v2_invalid_structure_and_transforms_rejected(portable_v2_project, mutation):
    root = ET.fromstring(formats.export_project(*portable_v2_project))
    frame = root.find("timeline/keyframe")
    layer = frame.find("layer")
    if mutation == "missing_setting":
        del root.find("settings").attrib["squash"]
    elif mutation == "extra_setting":
        root.find("settings").set("script", "run")
    elif mutation == "timeline_fps":
        root.find("timeline").set("fps", "24")
    elif mutation == "missing_layer":
        frame.remove(layer)
    elif mutation == "duplicate_layer":
        frame.append(deepcopy(layer))
    elif mutation == "unknown_layer":
        layer.set("name", "background")
    elif mutation == "unknown_layer_attribute":
        layer.set("path", "/tmp/untrusted.png")
    elif mutation == "nested_layer":
        ET.SubElement(layer, "layer")
    elif mutation == "layer_time":
        layer.set("time", "0")
    elif mutation == "layer_text":
        layer.text = "unexpected"
    elif mutation == "layer_tail":
        layer.tail = "unexpected"
    elif mutation == "layer_scale":
        layer.set("scale_x", "0")
    elif mutation == "layer_rotation":
        layer.set("rotation", "nan")
    elif mutation == "frame_scale_x":
        frame.set("scale_x", "inf")
    elif mutation == "missing_scale_y":
        del frame.attrib["scale_y"]
    elif mutation == "missing_layers":
        for item in list(frame):
            frame.remove(item)
    elif mutation == "layer_json":
        frame.set("layers", '{"body": {"x": 0}}')
    elif mutation == "unknown_frame_element":
        layer.tag = "command"
    elif mutation == "rig_rect":
        root.find("rig/head-rect").set("width", "1")
    elif mutation == "rig_duplicate":
        root.find("rig").append(deepcopy(root.find("rig/head-rect")))
    elif mutation == "rig_missing":
        root.find("rig").remove(root.find("rig/head-rect"))
    elif mutation == "rig_path":
        root.find("rig").set("path", "/tmp/untrusted.png")
    elif mutation == "time_order":
        root.find("timeline")[1].set("time", "0")
    elif mutation == "duration":
        root.find("timeline").set("duration", "31")
    elif mutation == "root_text":
        root.text = "unexpected"
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))


@pytest.mark.parametrize("fps", [24, 30, 60])
def test_v2_supports_full_thirty_second_timeline(portable_v2_project, fps):
    project, image, audio = portable_v2_project
    project["duration"] = 30
    project["settings"]["fps"] = fps
    frame = project["keyframes"][0]
    project["keyframes"] = [{**frame, "time": index / fps} for index in range(30 * fps + 1)]
    imported = formats.import_project(formats.export_project(project, image, audio))
    assert len(imported["keyframes"]) == 30 * fps + 1
    assert imported["keyframes"][-1]["time"] == 30


def test_frame_limit_on_import_and_export(portable_v2_project):
    project, image, audio = portable_v2_project
    root = ET.fromstring(formats.export_project(project, image, audio))
    timeline = root.find("timeline")
    for _ in range(formats.MAX_FRAMES):
        timeline.append(deepcopy(timeline[0]))
    with pytest.raises(ValueError, match="Quantidade de keyframes"):
        formats.import_project(ET.tostring(root))
    project["keyframes"] = [project["keyframes"][0]] * (formats.MAX_FRAMES + 1)
    with pytest.raises(ValueError, match="Quantidade de keyframes"):
        formats.export_project(project, image, audio)


@pytest.mark.parametrize("frame_field", ["time", "x", "y", "rotation", "scale"])
def test_boolean_frames_rejected_on_export(portable_project, frame_field):
    project, image, audio = portable_project
    project["keyframes"][0][frame_field] = True
    with pytest.raises(ValueError):
        formats.export_project(project, image, audio)


def test_v1_rejects_v2_motion_and_rig_metadata(portable_project):
    root = ET.fromstring(formats.export_project(*portable_project))
    root.find("settings").set("motion", "auto")
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))
    root.find("settings").set("motion", "hard")
    ET.SubElement(root, "rig")
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))


@pytest.fixture
def portable_v2_foreground(portable_v2_project):
    project, image, audio = portable_v2_project
    project["segmentation"] = {
        "method": "uniform-background", "kind": "character", "confidence": 0.83,
        "head_rect": [0.2, 0.1, 0.4, 0.3], "rect": [0.1, 0.2, 0.4, 0.5],
        "available": True, "warning": "not serialized", "private_path": "/private/mask.png",
    }
    foreground = io.BytesIO()
    Image.new("RGBA", (32, 32), (255, 50, 0, 128)).save(foreground, format="PNG")
    return project, image, audio, foreground.getvalue()


def test_v2_preserves_exact_foreground_and_safe_segmentation_metadata(portable_v2_foreground):
    project, image, audio, foreground = portable_v2_foreground
    encoded = formats.export_project(project, image, audio, foreground_bytes=foreground)
    assert b"/private/" not in encoded
    assert b"not serialized" not in encoded
    imported = formats.import_project(encoded)
    assert imported["foreground"] == foreground
    assert imported["segmentation_info"] == {
        "method": "uniform-background", "kind": "character", "confidence": 0.83,
        "head_rect": [0.2, 0.1, 0.4, 0.3],
    }
    assert imported["keyframes"] == project["keyframes"]
    assert Image.open(io.BytesIO(imported["foreground"])).getpixel((0, 0))[3] == 128


@pytest.mark.parametrize("method", [
    "alpha", "uniform-background", "face-grabcut", "manual-grabcut", "unavailable", "none"
])
def test_v2_all_known_segmentation_methods_can_round_trip(portable_v2_foreground, method):
    project, image, audio, foreground = portable_v2_foreground
    project["segmentation"].update({"method": method, "head_rect": None, "kind": "unknown"})
    imported = formats.import_project(formats.export_project(
        project, image, audio, foreground_bytes=foreground
    ))
    assert imported["segmentation_info"] == {
        "method": method, "kind": "unknown", "confidence": 0.83,
    }


@pytest.mark.parametrize("mutation", [
    "duplicate_foreground", "unknown_asset", "foreground_mime", "foreground_encoding",
    "foreground_base64", "foreground_audio", "foreground_path", "foreground_nested",
    "foreground_missing_audio", "metadata_method", "metadata_kind", "metadata_nan",
    "metadata_confidence", "metadata_missing", "metadata_extra", "metadata_text",
    "metadata_duplicate", "head_rect_duplicate", "head_rect_unknown", "head_rect_bounds",
    "head_rect_attribute", "head_rect_nested",
])
def test_v2_invalid_foreground_or_segmentation_metadata_rejected(portable_v2_foreground, mutation):
    project, image, audio, foreground = portable_v2_foreground
    root = ET.fromstring(formats.export_project(
        project, image, audio, foreground_bytes=foreground
    ))
    assets = root.find("assets")
    asset = assets.find("foreground")
    info = root.find("segmentation-info")
    head_rect = info.find("head-rect")
    if mutation == "duplicate_foreground":
        assets.append(deepcopy(asset))
    elif mutation == "unknown_asset":
        asset.tag = "script"
    elif mutation == "foreground_mime":
        asset.set("mime", "audio/wav")
    elif mutation == "foreground_encoding":
        asset.set("encoding", "path")
    elif mutation == "foreground_base64":
        asset.text = "invalid$$$"
    elif mutation == "foreground_audio":
        asset.text = assets.find("audio").text
    elif mutation == "foreground_path":
        asset.set("path", "/tmp/foreground.png")
    elif mutation == "foreground_nested":
        ET.SubElement(asset, "nested")
    elif mutation == "foreground_missing_audio":
        assets.remove(assets.find("audio"))
    elif mutation == "metadata_method":
        info.set("method", "exec")
    elif mutation == "metadata_kind":
        info.set("kind", "face-detector")
    elif mutation == "metadata_nan":
        info.set("confidence", "nan")
    elif mutation == "metadata_confidence":
        info.set("confidence", "1.01")
    elif mutation == "metadata_missing":
        del info.attrib["kind"]
    elif mutation == "metadata_extra":
        info.set("path", "/tmp/mask.png")
    elif mutation == "metadata_text":
        info.text = "unexpected"
    elif mutation == "metadata_duplicate":
        root.append(deepcopy(info))
    elif mutation == "head_rect_duplicate":
        info.append(deepcopy(head_rect))
    elif mutation == "head_rect_unknown":
        head_rect.tag = "script"
    elif mutation == "head_rect_bounds":
        head_rect.set("width", "1")
    elif mutation == "head_rect_attribute":
        head_rect.set("path", "/tmp/head.png")
    elif mutation == "head_rect_nested":
        ET.SubElement(head_rect, "nested")
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))


def test_v1_ignores_optional_foreground_and_rejects_new_xml_sections(portable_project):
    expected = formats.export_project(*portable_project)
    assert formats.export_project(*portable_project, foreground_bytes=b"ignored") == expected
    root = ET.fromstring(expected)
    ET.SubElement(root.find("assets"), "foreground", {"encoding": "base64", "mime": "image/png"})
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))
    root = ET.fromstring(expected)
    ET.SubElement(root, "segmentation-info", {"method": "alpha", "kind": "head", "confidence": "1"})
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))


def test_foreground_respects_asset_and_total_xml_size_limits(portable_v2_foreground, monkeypatch):
    project, image, audio, foreground = portable_v2_foreground
    no_foreground = formats.export_project(project, image, audio)
    with_foreground = formats.export_project(project, image, audio, foreground_bytes=foreground)
    with monkeypatch.context() as context:
        context.setattr(formats, "MAX_XML_BYTES", len(no_foreground) + 1)
        with pytest.raises(ValueError, match="56 MiB"):
            formats.export_project(project, image, audio, foreground_bytes=foreground)
        with pytest.raises(ValueError, match="56 MiB"):
            formats.import_project(with_foreground)
    with monkeypatch.context() as context:
        context.setattr(formats, "MAX_ASSET_BYTES", len(audio))
        with pytest.raises(ValueError, match="20 MiB"):
            formats.export_project(
                project, image, audio, foreground_bytes=foreground + b"x" * len(audio)
            )


def test_export_rejects_non_png_foreground(portable_v2_project):
    with pytest.raises(ValueError, match="PNG"):
        formats.export_project(*portable_v2_project, foreground_bytes=b"invalid")


@pytest.mark.parametrize("attribute", ["scale", "scale_x", "scale_y"])
@pytest.mark.parametrize("value", ["0.05", "9"])
@pytest.mark.parametrize("target", ["timeline/keyframe", "timeline/keyframe/layer"])
def test_scales_outside_renderer_bounds_rejected(portable_v2_project, attribute, value, target):
    root = ET.fromstring(formats.export_project(*portable_v2_project))
    root.find(target).set(attribute, value)
    with pytest.raises(ValueError):
        formats.import_project(ET.tostring(root))


def test_v1_retains_original_frame_limit(portable_project):
    project, image, audio = portable_project
    root = ET.fromstring(formats.export_project(project, image, audio))
    timeline = root.find("timeline")
    for _ in range(721):
        timeline.append(deepcopy(timeline[0]))
    assert len(timeline) == 723
    with pytest.raises(ValueError, match="Quantidade de keyframes"):
        formats.import_project(ET.tostring(root))
    project["keyframes"] = [project["keyframes"][0]] * 723
    with pytest.raises(ValueError, match="Quantidade de keyframes"):
        formats.export_project(project, image, audio)
