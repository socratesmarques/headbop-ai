import io
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from headbop.main import create_app


@pytest.fixture
def media():
    image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((32, 20, 96, 108), fill=(200, 230, 20, 255))
    image_data = io.BytesIO()
    image.save(image_data, format="PNG")
    sr = 22050
    samples = np.zeros(sr * 4, dtype=np.float32)
    for i, t in enumerate(np.arange(0.25, 4, 0.5)):
        start = round(t * sr)
        count = round(0.035 * sr)
        pulse = np.random.default_rng(i).normal(size=count) * np.exp(-np.arange(count) / 180)
        samples[start:start + count] = pulse * 0.4
    audio_data = io.BytesIO()
    sf.write(audio_data, samples, sr, format="WAV")
    return image_data.getvalue(), audio_data.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADBOP_ENV", "development")
    monkeypatch.delenv("HEADBOP_PUBLIC_ORIGIN", raising=False)
    app = create_app(tmp_path / "data")
    app.state.auth.create_user("alice", "correct-horse-battery-staple")
    with TestClient(app) as test_client:
        test_client.headers["Origin"] = "http://testserver"
        response = test_client.post("/api/auth/login", json={
            "username": "alice", "password": "correct-horse-battery-staple",
        })
        assert response.status_code == 200, response.text
        test_client.headers["X-CSRF-Token"] = response.json()["csrf"]
        yield test_client


def upload(client, media):
    response = client.post("/api/projects", files={
        "image": ("image.png", media[0], "image/png"),
        "audio": ("audio.wav", media[1], "audio/wav"),
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_static(client):
    assert client.get("/api/health").json() == {"status": "ok", "ffmpeg": True}
    page = client.get("/")
    assert page.status_code == 200
    assert "Headbop" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == "Headbop AI"


def test_complete_upload_render_export_import_delete(client, media):
    project = upload(client, media)
    prefix = f'/api/projects/{project["id"]}'
    assert 110 < project["analysis"]["bpm"] < 130
    assert len(project["keyframes"]) == 97
    assert project["segmentation"]["available"]
    assert client.get(project["image_url"]).content.startswith(b"\x89PNG")
    settings = {"mode": "subject", "motion": "velocity", "bpm": 100, "intensity": 1.3}
    update = client.put(prefix + "/settings", json=settings)
    assert update.status_code == 200, update.text
    assert update.json()["keyframes"] != project["keyframes"]
    assert client.get(prefix + "/video").status_code == 409
    response = client.post(prefix + "/render")
    assert response.status_code == 202
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.get(prefix + "/render").json()
        assert isinstance(result["progress"], int)
        assert 0 <= result["progress"] <= 100
        if result["status"] in {"ready", "error"}:
            break
        time.sleep(0.25)
    assert result["status"] == "ready", result
    assert result["progress"] == 100
    preview = client.get(result["video_url"])
    download = client.get(result["video_url"] + "?download=true")
    assert preview.content == download.content
    assert b"ftyp" in preview.content[:32]
    assert "attachment" in download.headers["content-disposition"]
    exported = client.get(prefix + "/export")
    assert exported.status_code == 200
    assert b"headbop-project" in exported.content
    imported = client.post("/api/import", files={"file": ("project.xml", exported.content)})
    assert imported.status_code == 201, imported.text
    restored = imported.json()
    assert restored["settings"] == update.json()["settings"]
    assert restored["keyframes"] == update.json()["keyframes"]
    assert restored["segmentation"]["method"] == project["segmentation"]["method"]
    assert client.delete(prefix).status_code == 204
    assert client.get(prefix).status_code == 404


def test_bad_media_does_not_leave_project(client, media):
    result = client.post("/api/projects", files={
        "image": ("bad.png", b"invalid"), "audio": ("beat.wav", media[1]),
    })
    assert result.status_code == 422
    assert not [path for path in client.app.state.store.root.iterdir() if path.is_dir()]


def test_invalid_settings_selection_and_xml(client, media):
    project = upload(client, media)
    prefix = f'/api/projects/{project["id"]}'
    assert client.put(prefix + "/settings", json={"bpm": 0}).status_code == 422
    assert client.post(prefix + "/segment", json={"rect": [-1, 0, 0.5, 0.5]}).status_code == 422
    assert client.post("/api/import", files={"file": ("other.xml", b"<scene/>")}).status_code == 422
    assert client.get("/api/projects/not-a-uuid").status_code == 404


def test_request_size_guard(client):
    response = client.post("/api/import", content=b"x", headers={"content-length": str(100 * 1024 * 1024)})
    assert response.status_code == 413


def test_audio_playlist_cannot_read_local_files(client, media):
    response = client.post("/api/projects", files={
        "image": ("image.png", media[0]),
        "audio": ("playlist.m3u8", b"#EXTM3U\n#EXTINF:2,\nfile:///etc/passwd\n"),
    })
    assert response.status_code == 422


def test_failed_automatic_crop_still_allows_whole_image(client, media, monkeypatch):
    def fail(*args):
        raise ValueError("Unable to segment")
    monkeypatch.setattr("headbop.main.segment_image", fail)
    project = upload(client, media)
    assert not project["segmentation"]["available"]
    assert project["settings"]["mode"] == "whole"
    exported = client.get(f'/api/projects/{project["id"]}/export')
    assert exported.status_code == 200, exported.text
    imported = client.post("/api/import", files={"file": ("fallback.xml", exported.content)})
    assert imported.status_code == 201, imported.text
    assert imported.json()["settings"]["mode"] == "whole"
    assert imported.json()["keyframes"] == project["keyframes"]


def test_render_failure_releases_project(client, media, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Test encoding failure")
    monkeypatch.setattr("headbop.main.run_render_job", fail)
    project = upload(client, media)
    prefix = f'/api/projects/{project["id"]}'
    client.post(prefix + "/render")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(prefix + "/render").json()
        if status["status"] == "error":
            break
        time.sleep(0.02)
    assert status["status"] == "error"
    assert client.delete(prefix).status_code == 204


def test_actual_body_size_guard(client, monkeypatch):
    monkeypatch.setattr("headbop.main.MAX_XML", 8)
    response = client.post("/api/import", content=b"x" * 30,
                           headers={"content-length": "1", "content-type": "multipart/form-data; boundary=a"})
    assert response.status_code == 413


def test_new_project_auto_and_capabilities(client, media):
    project = upload(client, media)
    assert project["settings"]["engine"] == "v2"
    assert project["settings"]["motion"] == "auto"
    assert project["settings"]["mode"] == "subject"  # Precise supplied alpha.
    assert project["capabilities"]["reference_available"] is False
    assert project["mask_url"] == project["foreground_url"]
    assert "layers" in project["keyframes"][0]
    assert "energy" in project["analysis"]


def test_legacy_project_read_and_xml_roundtrip_preserve_frames(client, media):
    from headbop.animation import generate_keyframes
    project = upload(client, media)
    store = client.app.state.store
    original = store.read(project["id"])
    original["settings"] = {"mode": "whole", "motion": "hard", "bpm": 120, "intensity": 1}
    original.pop("rig", None)
    for key in ("kind", "confidence", "head_rect"):
        original["segmentation"].pop(key, None)
    original["keyframes"] = generate_keyframes(4, original["analysis"]["beats"],
                                               original["analysis"]["intensity"], 120, 1, "hard")
    # Imported timelines must survive exactly, including manually edited samples.
    original["keyframes"][5]["rotation"] = 3.14159
    store.write(original)
    prefix = f'/api/projects/{project["id"]}'
    assert client.get(prefix).json()["settings"]["engine"] == "legacy"
    assert "engine" not in store.read(project["id"])["settings"]  # Read is non-mutating.
    assert "kind" not in store.read(project["id"])["segmentation"]
    xml = client.get(prefix + "/export")
    assert b'<headbop-project version="1">' in xml.content
    imported = client.post("/api/import", files={"file": ("legacy.xml", xml.content)})
    assert imported.status_code == 201, imported.text
    assert imported.json()["keyframes"] == original["keyframes"]
    assert imported.json()["settings"]["engine"] == "legacy"
    upgraded = client.put(prefix + "/settings", json={**original["settings"], "engine": "v2"})
    assert upgraded.status_code == 200, upgraded.text
    xml_v2 = client.get(prefix + "/export")
    assert xml_v2.status_code == 200, xml_v2.text
    restored = client.post("/api/import", files={"file": ("upgraded.xml", xml_v2.content)})
    assert restored.status_code == 201, restored.text
    assert restored.json()["keyframes"] == upgraded.json()["keyframes"]
    assert client.get(restored.json()["mask_url"]).content == client.get(project["mask_url"]).content


def test_v2_output_controls_manual_rig_and_export(client, media):
    project = upload(client, media)
    prefix = f'/api/projects/{project["id"]}'
    selected = {**project["settings"], "fps": 60, "resolution": 1080, "aspect": "vertical",
                "motion": "smooth", "speed": 1.4, "easing": "ease-in-out", "squash": 0.7}
    changed = client.put(prefix + "/settings", json=selected)
    assert changed.status_code == 200, changed.text
    assert len(changed.json()["keyframes"]) == 241
    response = client.post(prefix + "/rig", json={"head_rect": [0.23, 0.12, 0.54, 0.4]})
    assert response.status_code == 200, response.text
    rigged = response.json()
    assert rigged["rig"]["head_rect"] == [0.23, 0.12, 0.54, 0.4]
    xml = client.get(prefix + "/export")
    assert xml.status_code == 200, xml.text
    assert b'<headbop-project version="2">' in xml.content
    imported = client.post("/api/import", files={"file": ("v2.xml", xml.content)})
    assert imported.status_code == 201, imported.text
    restored = imported.json()
    assert restored["settings"] == selected
    assert restored["keyframes"] == rigged["keyframes"]
    assert restored["rig"]["head_rect"] == rigged["rig"]["head_rect"]


@pytest.mark.parametrize("option", [
    {"fps": 120}, {"resolution": 2160}, {"aspect": "crop"}, {"speed": 0},
    {"squash": 1.1}, {"motion": "reference"}, {"easing": "exec"},
    {"engine": "legacy", "motion": "auto"},
])
def test_v2_rejects_unsupported_options(client, option):
    owner_id = client.get("/api/auth/session").json()["user"]["id"]
    project_id, _ = client.app.state.store.create(owner_id=owner_id)
    client.app.state.store.building.discard(project_id)
    response = client.put(f"/api/projects/{project_id}/settings", json={"bpm": 120, **option})
    assert response.status_code == 422


def test_xml_duration_must_match_embedded_audio(client, media):
    from xml.etree import ElementTree as ET
    project = upload(client, media)
    xml = client.get(f'/api/projects/{project["id"]}/export').content
    root = ET.fromstring(xml)
    root.find("timeline").set("duration", "5")
    response = client.post("/api/import", files={"file": ("wrong.xml", ET.tostring(root))})
    assert response.status_code == 422


def test_manual_rig_invalid_selection_is_rejected(client, media):
    project = upload(client, media)
    response = client.post(f'/api/projects/{project["id"]}/rig', json={"head_rect": [0, 0, 2, 1]})
    assert response.status_code == 422


def test_v2_xml_preserves_uniform_background_mask_and_render(client, media):
    image = Image.new("RGBA", (128, 128), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((35, 10, 93, 68), fill="#ffbe75")
    draw.rectangle((55, 56, 73, 90), fill="#ffbe75")
    draw.rounded_rectangle((24, 77, 104, 120), radius=10, fill="#3157ce")
    data = io.BytesIO()
    image.save(data, format="PNG")
    project = upload(client, (data.getvalue(), media[1]))
    assert project["segmentation"]["method"] == "uniform-background"
    prefix = f'/api/projects/{project["id"]}'
    rigged = client.post(prefix + "/rig", json={"head_rect": [0.24, 0.04, 0.52, 0.52]}).json()
    assert rigged["rig"]["kind"] == "head-body"
    original_mask = client.get(rigged["foreground_url"]).content
    exported = client.get(prefix + "/export")
    assert exported.status_code == 200, exported.text
    restored = client.post("/api/import", files={"file": ("avatar.xml", exported.content)})
    assert restored.status_code == 201, restored.text
    restored = restored.json()
    assert restored["segmentation"]["method"] == "uniform-background"
    assert restored["segmentation"]["kind"] == rigged["segmentation"]["kind"]
    assert restored["rig"]["pivots"] == rigged["rig"]["pivots"]
    assert client.get(restored["foreground_url"]).content == original_mask
    videos = []
    for current in (rigged, restored):
        url = f'/api/projects/{current["id"]}'
        assert client.post(url + "/render").status_code == 202
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = client.get(url + "/render").json()
            if status["status"] in {"ready", "error"}:
                break
            time.sleep(0.25)
        assert status["status"] == "ready", status
        videos.append(client.get(url + "/video").content)
    assert videos[0] == videos[1]
