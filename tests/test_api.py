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
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as test_client:
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
        if result["status"] in {"ready", "error"}:
            break
        time.sleep(0.05)
    assert result["status"] == "ready", result
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
    assert restored["settings"] == settings
    assert restored["keyframes"] == update.json()["keyframes"]
    assert restored["segmentation"]["method"] == project["segmentation"]["method"]
    assert client.delete(prefix).status_code == 204
    assert client.get(prefix).status_code == 404


def test_bad_media_does_not_leave_project(client, media):
    result = client.post("/api/projects", files={
        "image": ("bad.png", b"invalid"), "audio": ("beat.wav", media[1]),
    })
    assert result.status_code == 422
    assert list(client.app.state.store.root.iterdir()) == []


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


def test_render_failure_releases_project(client, media, monkeypatch):
    def fail(*args):
        raise RuntimeError("Test encoding failure")
    monkeypatch.setattr("headbop.main.render_video", fail)
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
