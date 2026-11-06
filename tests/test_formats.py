import io
import wave
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from headbop import formats


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
    assert imported == {"settings": project["settings"], "image": image, "audio": audio,
                        "rect": project["segmentation"]["rect"]}
    assert Image.open(io.BytesIO(imported["image"])).size == (32, 32)
    with wave.open(io.BytesIO(imported["audio"])) as wav:
        assert wav.getnframes() == 22050
    root = ET.fromstring(encoded)
    assert len(root.find("timeline")) == 2


@pytest.mark.parametrize("payload", [
    b"<scene title='Alight Motion'/>", b"<headbop-project version='2'/>", b"<project/>",
])
def test_foreign_and_unknown_versions_are_explicitly_rejected(payload):
    with pytest.raises(ValueError, match="Alight Motion"):
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
