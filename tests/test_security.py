"""HTTP boundary tests: authenticate and admit work before reading client bodies."""

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from headbop.auth import AuthBusyError, AuthStore
from headbop.main import create_app

PASSWORD = "correct-horse-battery-staple"
ORIGIN = "http://testserver"


@pytest.fixture(scope="module")
def accounts_database(tmp_path_factory):
    # Reuse real password hashes while giving each test an isolated SQLite copy.
    path = tmp_path_factory.mktemp("accounts") / "security.sqlite3"
    store = AuthStore(path)
    store.create_user("alice", PASSWORD)
    store.create_user("bob", PASSWORD)
    return path


@pytest.fixture
def application(tmp_path, monkeypatch, accounts_database):
    monkeypatch.setenv("HEADBOP_ENV", "development")
    monkeypatch.delenv("HEADBOP_PUBLIC_ORIGIN", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(accounts_database, data / "security.sqlite3")
    return create_app(data)


@pytest.fixture
def anonymous(application):
    with TestClient(application) as client:
        client.headers["Origin"] = ORIGIN
        yield client


def login(client, username="alice", password=PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return response


def project_for(app, username="alice"):
    user = app.state.auth.get_user(username)
    store = app.state.store
    project_id, path = store.create(owner_id=user["id"])
    project = {
        "id": project_id, "width": 64, "height": 64, "duration": 1,
        "analysis": {"beats": [], "intensity": []},
        "segmentation": {"available": False},
        "settings": {"engine": "legacy", "mode": "whole", "motion": "hard",
                     "bpm": 120, "intensity": 1},
        "keyframes": [], "image_url": f"/api/projects/{project_id}/image",
        "foreground_url": None,
        "render": {"status": "idle", "video_url": None, "error": None},
    }
    store.write(project)
    store.building.discard(project_id)
    return project_id, path


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/auth/session"), ("GET", "/api/projects"),
    ("GET", "/api/projects/" + "a" * 32),
    ("GET", "/api/projects/" + "a" * 32 + "/image"),
    ("GET", "/api/projects/" + "a" * 32 + "/video"),
    ("GET", "/api/projects/" + "a" * 32 + "/export"),
    ("POST", "/api/projects"), ("POST", "/api/import"),
    ("DELETE", "/api/projects/" + "a" * 32),
    ("GET", "/openapi.json"),
])
def test_unauthenticated_api_is_denied_before_parsing(anonymous, method, path):
    response = anonymous.request(method, path, content=b"invalid multipart")
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "auth_required"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_login_session_logout_cookie_contract(anonymous):
    response = login(anonymous)
    body = response.json()
    assert set(body) == {"user", "csrf", "expires_at"}
    assert body["user"]["username"] == "alice"
    assert len(body["csrf"]) >= 32
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "token" not in body
    assert anonymous.get("/api/auth/session").json() == body
    session_token = anonymous.cookies.get("headbop_session")
    assert session_token and anonymous.app.state.auth.get_session(session_token)
    response = anonymous.post("/api/auth/logout")
    assert response.status_code in (200, 204)
    assert anonymous.app.state.auth.get_session(session_token) is None
    assert anonymous.get("/api/auth/session").status_code == 401


@pytest.mark.parametrize("suffix,method", [
    ("", "GET"), ("", "DELETE"), ("/image", "GET"), ("/foreground", "GET"),
    ("/video", "GET"), ("/export", "GET"), ("/render", "GET"),
    ("/render", "POST"), ("/settings", "PUT"), ("/segment", "POST"), ("/rig", "POST"),
])
def test_every_project_route_checks_owner_before_validation(anonymous, suffix, method):
    project_id, path = project_for(anonymous.app, "bob")
    before = (path / "project.json").read_bytes()
    login(anonymous, "alice")
    response = anonymous.request(method, f"/api/projects/{project_id}{suffix}", content=b"bad JSON")
    missing = anonymous.request(method, f"/api/projects/{'f' * 32}{suffix}", content=b"bad JSON")
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json()
    assert (path / "project.json").read_bytes() == before


def test_project_list_only_contains_current_user_and_no_private_fields(anonymous):
    alice_id, _ = project_for(anonymous.app, "alice")
    project_for(anonymous.app, "bob")
    login(anonymous)
    response = anonymous.get("/api/projects")
    assert response.status_code == 200
    projects = response.json()["projects"]
    assert len(projects) == 1 and projects[0]["id"] == alice_id
    assert set(projects[0]) == {"id", "width", "height", "duration", "updated_at"}
    own_project = anonymous.get(f"/api/projects/{alice_id}")
    assert own_project.status_code == 200
    assert "owner_id" not in own_project.json()
    assert "render" not in own_project.json()


def test_ownerless_legacy_projects_are_not_silently_assigned(anonymous):
    store = anonymous.app.state.store
    project_id, path = store.create()
    store.building.discard(project_id)
    (path / "project.json").write_text('{"id":"' + project_id + '"}')
    original = (path / "project.json").read_bytes()
    login(anonymous)
    assert anonymous.get(f"/api/projects/{project_id}").status_code == 404
    assert anonymous.get("/api/projects").json() == {"projects": []}
    assert (path / "project.json").read_bytes() == original
    assert not (path / ".owner").exists()


@pytest.mark.parametrize("headers", [
    {"X-CSRF-Token": ""}, {"X-CSRF-Token": "wrong"},
    {"Origin": "https://attacker.example"}, {"Origin": "null"},
])
def test_csrf_and_cross_origin_writes_are_rejected_without_mutation(anonymous, headers):
    project_id, path = project_for(anonymous.app)
    login(anonymous)
    response = anonymous.delete(f"/api/projects/{project_id}", headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] in {"csrf_failed", "origin_forbidden"}
    assert path.exists()


def test_login_cross_origin_is_rejected(anonymous):
    response = anonymous.post("/api/auth/login", json={"username": "alice", "password": PASSWORD},
                              headers={"Origin": "https://attacker.example"})
    assert response.status_code == 403
    assert not anonymous.cookies.get("headbop_session")


def test_password_reset_revokes_active_browser_session(anonymous):
    login(anonymous)
    anonymous.app.state.auth.reset_password("alice", "replacement-password-with-entropy")
    assert anonymous.get("/api/auth/session").status_code == 401
    assert anonymous.get("/api/projects").status_code == 401


def test_disabled_account_cannot_read_existing_projects(anonymous):
    project_id, path = project_for(anonymous.app)
    login(anonymous)
    anonymous.app.state.auth.disable_user("alice")
    assert anonymous.get(f"/api/projects/{project_id}").status_code == 401
    assert path.exists()


def test_expired_session_cannot_read_api(anonymous, monkeypatch):
    body = login(anonymous).json()
    monkeypatch.setattr(anonymous.app.state.auth, "clock", lambda: body["expires_at"] + 1)
    assert anonymous.get("/api/auth/session").status_code == 401


def test_invalid_credentials_do_not_reveal_account_existence(anonymous):
    responses = [anonymous.post("/api/auth/login", json={"username": name, "password": "bad"})
                 for name in ("alice", "nonexistent", "!invalid")]
    assert all(response.status_code == 401 for response in responses)
    assert len({response.text for response in responses}) == 1
    assert responses[0].json()["code"] == "invalid_credentials"


def test_login_rate_limit_cannot_be_bypassed_with_forged_forwarded_header(anonymous, monkeypatch):
    attempts = []
    def reject(username, password):
        attempts.append(username)
        return None
    monkeypatch.setattr(anonymous.app.state.auth, "login", reject)
    responses = [anonymous.post("/api/auth/login", json={"username": f"user{index}", "password": "bad"},
                                headers={"X-Forwarded-For": f"203.0.113.{index}"})
                 for index in range(12)]
    limited = [response for response in responses if response.status_code == 429]
    assert limited, [response.status_code for response in responses]
    assert all(int(response.headers["retry-after"]) > 0 for response in limited)
    assert len(attempts) < len(responses), "Rate limit must precede expensive password hashing"


def test_busy_password_hash_capacity_returns_bounded_retry(anonymous, monkeypatch):
    def busy(*args, **kwargs):
        raise AuthBusyError("auth.busy")
    monkeypatch.setattr(anonymous.app.state.auth, "login", busy)
    response = anonymous.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert response.status_code in (429, 503)
    assert response.json()["code"] == "auth_busy"
    assert int(response.headers["retry-after"]) > 0


@pytest.mark.parametrize("origin", [None, "http://public.example", "https://public.example/path",
                                    "https://user:secret@public.example", "https://public.example?query=1"])
def test_production_rejects_missing_or_invalid_public_origin(tmp_path, monkeypatch, origin):
    monkeypatch.setenv("HEADBOP_ENV", "production")
    if origin is None:
        monkeypatch.delenv("HEADBOP_PUBLIC_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("HEADBOP_PUBLIC_ORIGIN", origin)
    with pytest.raises((ValueError, RuntimeError)):
        with TestClient(create_app(tmp_path / "data")):
            pass


def test_production_cookie_secure_and_origin_host_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADBOP_ENV", "production")
    monkeypatch.setenv("HEADBOP_PUBLIC_ORIGIN", "https://headbop.example")
    app = create_app(tmp_path / "data")
    app.state.auth.create_user("alice", PASSWORD)
    with TestClient(app, base_url="https://headbop.example") as client:
        client.headers["Origin"] = "https://headbop.example"
        response = login(client)
        assert "secure" in response.headers["set-cookie"].lower()
        assert client.get("/api/projects").status_code == 200
        spoofed = client.get("/api/projects", headers={"Host": "attacker.example"})
        assert spoofed.status_code in (400, 403)


def test_render_timeout_releases_queue_and_allows_retry(anonymous, monkeypatch):
    project_id, _ = project_for(anonymous.app)
    login(anonymous)
    calls = []
    def timeout(path, **kwargs):
        calls.append(path)
        raise TimeoutError("Encoder deliberately timed out")
    monkeypatch.setattr("headbop.main.run_render_job", timeout)
    prefix = f"/api/projects/{project_id}"
    for _ in range(2):
        assert anonymous.post(prefix + "/render").status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = anonymous.get(prefix + "/render").json()
            if result["status"] == "error":
                break
            time.sleep(0.05)
        assert result["status"] == "error"
        assert result["progress"] < 100
        assert project_id not in anonymous.app.state.store.active
    assert len(calls) == 2
    assert anonymous.delete(prefix).status_code == 204


def test_project_quota_is_checked_before_parsing_and_does_not_affect_other_users(anonymous):
    store = anonymous.app.state.store
    store.user_max_projects = 1
    project_for(anonymous.app, "alice")
    login(anonymous)
    response = anonymous.post("/api/projects", content=b"invalid multipart")
    assert response.status_code in (429, 422)
    assert response.json()["code"] == "quota_projects"
    assert store.reservations == {}
    login(anonymous, "bob")
    response = anonymous.post("/api/projects", content=b"invalid multipart")
    assert response.status_code == 422
    assert response.json().get("code") != "quota_projects"
    assert store.reservations == {}


def test_storage_budget_is_reserved_before_parsing_import(anonymous):
    store = anonymous.app.state.store
    store.user_storage = 64 * 1024 * 1024
    login(anonymous)
    response = anonymous.post("/api/import", content=b"invalid multipart")
    assert response.status_code in (429, 422)
    assert response.json()["code"] == "quota_storage"
    assert store.reservations == {}


def test_malformed_upload_releases_admission_and_reserved_space(anonymous):
    login(anonymous)
    for endpoint in ("/api/projects", "/api/import", "/api/projects"):
        response = anonymous.post(endpoint, content=b"invalid multipart")
        assert response.status_code == 422, response.text
        assert anonymous.app.state.store.reservations == {}


def test_render_queue_is_fair_and_progress_waits_for_encoder_finalization(anonymous, monkeypatch):
    import threading

    first, _ = project_for(anonymous.app, "alice")
    second, _ = project_for(anonymous.app, "alice")
    bob_project, _ = project_for(anonymous.app, "bob")
    login(anonymous)
    started = threading.Event()
    finish_frames = threading.Event()
    finalizing = threading.Event()
    finish_job = threading.Event()

    def controlled(path, progress_callback, **kwargs):
        progress_callback(5, 10)
        started.set()
        assert finish_frames.wait(5)
        progress_callback(10, 10)
        finalizing.set()
        assert finish_job.wait(5)
        raise RuntimeError("Deliberate encoder failure after frames")

    monkeypatch.setattr("headbop.main.run_render_job", controlled)
    prefix = f"/api/projects/{first}"
    try:
        assert anonymous.post(prefix + "/render").status_code == 202
        assert started.wait(5)
        status = anonymous.get(prefix + "/render").json()
        assert status["status"] == "rendering" and status["progress"] == 50
        assert status["timeout_seconds"] == 630
        denied = anonymous.post(f"/api/projects/{second}/render")
        assert denied.status_code == 429
        # Reuse the running application without entering its lifespan twice.
        other = TestClient(anonymous.app)
        other.headers["Origin"] = ORIGIN
        try:
            login(other, "bob")
            queued = other.post(f"/api/projects/{bob_project}/render")
            assert queued.status_code == 202, queued.text
            assert queued.json()["timeout_seconds"] == 630
            status = other.get(f"/api/projects/{bob_project}/render").json()
            assert status["status"] == "queued" and status["progress"] == 0
            assert status["timeout_seconds"] == queued.json()["timeout_seconds"]
            finish_frames.set()
            assert finalizing.wait(5)
            status = anonymous.get(prefix + "/render").json()
            assert status["status"] == "rendering" and status["progress"] == 95
            assert anonymous.get(prefix + "/video").status_code == 409
            finish_job.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and anonymous.app.state.store.active:
                time.sleep(0.05)
            assert not anonymous.app.state.store.active
            assert anonymous.app.state.store.reservations == {}
            assert anonymous.get(prefix + "/render").json()["status"] == "error"
        finally:
            other.close()
    finally:
        finish_frames.set()
        finish_job.set()


def test_second_application_on_same_data_root_is_rejected(application):
    second = create_app(application.state.store.root)
    with TestClient(application) as first:
        with pytest.raises(RuntimeError, match="one application worker"):
            with TestClient(second):
                pass
        assert first.get("/api/health").status_code == 200
    # Graceful shutdown releases the OS lock for an ordinary restart.
    with TestClient(second) as restarted:
        assert restarted.get("/api/health").status_code == 200


def test_non_ascii_csrf_header_is_rejected_without_server_error(anonymous):
    project_id, path = project_for(anonymous.app)
    login(anonymous)
    response = anonymous.delete(f"/api/projects/{project_id}",
                                headers={b"x-csrf-token": b"\xff"})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert path.exists()


def test_malformed_unicode_login_is_rejected_without_server_error(anonymous):
    response = anonymous.post("/api/auth/login", content=b'{"username":"\\ud800","password":"bad"}',
                              headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_missing_ready_output_never_reports_complete_and_can_retry(anonymous, monkeypatch):
    project_id, path = project_for(anonymous.app)
    store = anonymous.app.state.store
    project = store.read(project_id)
    project["render"] = {"status": "ready", "video_url": f"/api/projects/{project_id}/video", "error": None}
    store.write(project)
    login(anonymous)
    prefix = f"/api/projects/{project_id}"
    response = anonymous.get(prefix + "/render")
    assert response.json()["status"] == "error"
    assert response.json()["progress"] < 100
    calls = []
    def render(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("test")
    monkeypatch.setattr("headbop.main.run_render_job", render)
    assert anonymous.post(prefix + "/render").status_code == 202
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and store.active:
        time.sleep(0.01)
    assert calls
    assert not store.active


def test_audio_decoder_does_not_buffer_untrusted_diagnostics(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from headbop.main import normalize_audio

    calls = []
    def invalid_audio(command, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr("headbop.main.subprocess.run", invalid_audio)
    with pytest.raises(ValueError, match="Áudio inválido"):
        normalize_audio(b"malformed media", tmp_path / "audio.wav")
    assert calls[0]["stdout"] == calls[0]["stderr"] == subprocess.DEVNULL
    assert calls[0]["timeout"] == 45
    assert not (tmp_path / "upload.audio").exists()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 1801])
def test_invalid_render_budget_refuses_startup(tmp_path, monkeypatch, timeout):
    monkeypatch.setenv("HEADBOP_ENV", "development")
    monkeypatch.setenv("HEADBOP_RENDER_TIMEOUT_SECONDS", str(timeout))
    with pytest.raises(ValueError, match="Render timeout"):
        with TestClient(create_app(tmp_path)):
            pass


def test_render_budget_tracks_server_configuration_for_ready_and_pending_projects(anonymous):
    app = anonymous.app
    app.state.config.render_timeout = 900
    project_id, path = project_for(app)
    login(anonymous)
    prefix = f"/api/projects/{project_id}"
    assert anonymous.get(prefix + "/render").json()["timeout_seconds"] == 1830
    # Reopening an already validated output must carry the same client contract.
    project = app.state.store.read(project_id)
    (path / "video.mp4").write_bytes(b"previously-validated-output")
    project["render"] = {"status": "ready", "video_url": prefix + "/video", "error": None}
    app.state.store.write(project)
    response = anonymous.post(prefix + "/render")
    assert response.status_code == 202
    assert response.json()["timeout_seconds"] == 1830
    assert response.json()["progress"] == 100
