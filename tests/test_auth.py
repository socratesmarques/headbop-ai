from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import sqlite3
import threading
import warnings

import pytest

from headbop import auth, manage
from headbop.auth import AuthBusyError, AuthStore, InvalidCredentialsError


PASSWORD = "a sufficiently long password"
NEW_PASSWORD = "a different long password"


@pytest.fixture
def clock():
    value = [1000.0]
    return value


@pytest.fixture
def store(tmp_path, clock):
    return AuthStore(tmp_path / "private" / "security.sqlite3",
                     session_ttl_seconds=60, clock=lambda: clock[0])


@pytest.fixture
def user(store):
    return store.create_user("Alice", PASSWORD)


def test_password_hash_uses_random_salt_and_real_scrypt():
    first, second = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
    assert first != second
    assert first.startswith("scrypt$131072$8$1$")
    assert PASSWORD not in first
    assert auth._verify_password(PASSWORD, first)
    assert not auth._verify_password(NEW_PASSWORD, first)


@pytest.mark.parametrize("username", ["ab", "x" * 65, " alice", "alice ",
                                      "alice@example.com", "alice' OR 1=1--", None])
def test_create_rejects_invalid_usernames(store, username):
    with pytest.raises(ValueError, match="auth.invalid_username"):
        store.create_user(username, PASSWORD)
    assert store.list_users() == []


@pytest.mark.parametrize("password", ["short", " " * 12, "x" * 129, None])
def test_create_rejects_invalid_passwords(store, password):
    with pytest.raises(ValueError, match="auth.invalid_password"):
        store.create_user("alice", password)
    assert store.list_users() == []


def test_users_are_normalized_unique_and_never_expose_passwords(store, user):
    assert user["username"] == "alice"
    assert store.get_user("ALICE") == user
    assert store.get_user("missing") is None
    assert store.list_users() == [user]
    with pytest.raises(ValueError, match="auth.username_taken"):
        store.create_user("ALICE", NEW_PASSWORD)
    with sqlite3.connect(store.path) as db:
        stored = db.execute("SELECT password_hash FROM users").fetchone()[0]
    assert stored != PASSWORD
    assert stored.startswith("scrypt$")
    assert "password" not in str(user)


def test_unknown_invalid_and_corrupt_credentials_still_derive(store, user, monkeypatch):
    calls = []

    def derive(password, salt):
        calls.append((password, salt))
        return b"\x00" * 32

    monkeypatch.setattr(auth, "_derive", derive)
    assert store.login("unknown", PASSWORD) is None
    assert store.login("' OR 1=1--", PASSWORD) is None
    assert store.login("alice", "wrong") is None
    assert store.login("alice", None) is None
    assert not auth._verify_password(PASSWORD, "scrypt$1$8$1$bad$bad")
    assert not auth._verify_password(PASSWORD, "scrypt$131072$8$1$!$!")
    assert len(calls) == 6
    assert calls[0][1] == calls[1][1] == calls[4][1] == calls[5][1]


def test_hashing_capacity_rejects_excess_work_and_recovers(monkeypatch):
    monkeypatch.setattr(auth, "_HASH_SLOTS", threading.BoundedSemaphore(2))
    entered, release = threading.Barrier(3), threading.Event()

    def blocked_scrypt(password, **kwargs):
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return b"x" * 32

    monkeypatch.setattr(auth.hashlib, "scrypt", blocked_scrypt)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(auth._derive, b"password", b"salt") for _ in range(2)]
        try:
            entered.wait(timeout=5)
            with pytest.raises(AuthBusyError):
                auth._derive(b"password", b"salt")
        finally:
            release.set()
        assert [future.result(timeout=5) for future in futures] == [b"x" * 32] * 2

    def broken_scrypt(*args, **kwargs):
        raise MemoryError("hash allocation failed")

    monkeypatch.setattr(auth.hashlib, "scrypt", broken_scrypt)
    with pytest.raises(MemoryError):
        auth._derive(b"password", b"salt")
    monkeypatch.setattr(auth.hashlib, "scrypt", lambda *args, **kwargs: b"y" * 32)
    assert auth._derive(b"password", b"salt") == b"y" * 32


def test_sessions_are_opaque_hashed_persisted_and_expire(store, user, clock):
    session = store.login("ALICE", PASSWORD)
    assert session["user"] == {"id": user["id"], "username": "alice"}
    assert len(session["token"]) == len(session["csrf"]) == 43
    assert session["token"] != session["csrf"]
    assert session["expires_at"] == 1060
    with sqlite3.connect(store.path) as db:
        stored = db.execute("SELECT * FROM sessions").fetchone()
    assert session["token"] not in stored
    assert stored[0] == hashlib.sha256(session["token"].encode()).hexdigest()
    restarted = AuthStore(store.path, session_ttl_seconds=60, clock=lambda: clock[0])
    expected = {key: value for key, value in session.items() if key != "token"}
    assert restarted.get_session(session["token"]) == expected
    clock[0] = 1059.9
    assert restarted.get_session(session["token"]) == expected
    clock[0] = 1060
    assert restarted.get_session(session["token"]) is None
    restarted.cleanup()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("token", [None, "", "x" * 42, "x" * 44, "!" * 43,
                                   "x" * 42 + "\n", "é" * 43, 123])
def test_invalid_session_tokens_are_rejected(store, token):
    assert store.get_session(token) is None
    store.revoke_session(token)


def test_logout_revokes_only_the_requested_session(store, user):
    first = store.create_session(user["id"])
    second = store.create_session(user["id"])
    store.revoke_session(first["token"])
    assert store.get_session(first["token"]) is None
    assert store.get_session(second["token"]) is not None


def test_concurrent_sessions_are_bounded(store, user):
    with ThreadPoolExecutor(max_workers=8) as pool:
        sessions = list(pool.map(lambda _: store.create_session(user["id"]), range(24)))
    assert len({session["token"] for session in sessions}) == 24
    assert sum(store.get_session(session["token"]) is not None for session in sessions) == 8
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 8


def test_reset_password_revokes_all_sessions_and_old_password(store, user):
    sessions = [store.create_session(user["id"]) for _ in range(2)]
    store.reset_password("ALICE", NEW_PASSWORD)
    assert all(store.get_session(session["token"]) is None for session in sessions)
    assert store.login("alice", PASSWORD) is None
    assert store.login("alice", NEW_PASSWORD) is not None


def test_disable_revokes_sessions_and_cannot_be_undone_by_password_reset(store, user):
    session = store.create_session(user["id"])
    store.disable_user("ALICE")
    store.reset_password("alice", NEW_PASSWORD)
    assert store.get_session(session["token"]) is None
    assert store.login("alice", NEW_PASSWORD) is None
    assert store.get_user("alice")["disabled"] is True
    with pytest.raises(InvalidCredentialsError):
        store.create_session(user["id"])


@pytest.mark.parametrize("operation", ["reset", "disable", "revoke"])
def test_revocation_during_login_cannot_issue_a_stale_session(store, user, monkeypatch,
                                                            operation):
    authenticated, continue_login = threading.Event(), threading.Event()
    original_authenticate = store.authenticate

    def paused_authenticate(*args):
        result = original_authenticate(*args)
        authenticated.set()
        assert continue_login.wait(timeout=10)
        return result

    monkeypatch.setattr(store, "authenticate", paused_authenticate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.login, "alice", PASSWORD)
        try:
            assert authenticated.wait(timeout=10)
            if operation == "reset":
                store.reset_password("alice", NEW_PASSWORD)
            elif operation == "disable":
                store.disable_user("alice")
            else:
                store.revoke_user_sessions(user["id"])
        finally:
            continue_login.set()
        assert pending.result(timeout=10) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_unknown_user_mutations_fail_without_creating_sessions(store):
    with pytest.raises(InvalidCredentialsError):
        store.create_session("unknown")
    with pytest.raises(ValueError, match="auth.user_not_found"):
        store.reset_password("unknown", PASSWORD)
    with pytest.raises(ValueError, match="auth.user_not_found"):
        store.disable_user("unknown")
    assert store.list_users() == []


def test_rate_limits_are_atomic_persistent_and_do_not_extend_on_rejection(store, clock):
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(lambda _: store.consume_rate("login:alice", 7, 60), range(40)))
    assert attempts.count(0) == 7
    assert attempts.count(60) == 33
    restarted = AuthStore(store.path, clock=lambda: clock[0])
    clock[0] += 10.2
    assert restarted.consume_rate("login:alice", 7, 60) == 50
    assert restarted.consume_rate("login:bob", 7, 60) == 0
    with sqlite3.connect(store.path) as db:
        buckets = db.execute("SELECT bucket,count FROM rate_limits ORDER BY count").fetchall()
    assert [count for _, count in buckets] == [1, 7]
    assert all("alice" not in bucket and "bob" not in bucket for bucket, _ in buckets)
    clock[0] = 1060
    assert restarted.consume_rate("login:alice", 7, 60) == 0
    clock[0] = 1200
    restarted.cleanup()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] == 0


@pytest.mark.parametrize("key,limit,window", [("", 1, 60), ("x" * 513, 1, 60),
                                              ("key", 0, 60), ("key", True, 60),
                                              ("key", 1, 0), ("key", 1, float("inf"))])
def test_invalid_rate_policies_are_rejected(store, key, limit, window):
    with pytest.raises(ValueError):
        store.consume_rate(key, limit, window)


@pytest.mark.parametrize("ttl", [0, 59, 604801, float("inf"), float("nan")])
def test_session_lifetime_is_bounded(tmp_path, ttl):
    with pytest.raises(ValueError):
        AuthStore(tmp_path / "security.sqlite3", session_ttl_seconds=ttl)


def test_database_permissions_and_symlink_rejection(store, tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permissions are enforced in production Linux containers")
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    target = tmp_path / "unrelated.txt"
    target.write_text("keep this file")
    link = tmp_path / "symlink.sqlite3"
    link.symlink_to(target)
    with pytest.raises(OSError):
        AuthStore(link)
    assert target.read_text() == "keep this file"


def test_management_cli_provisions_resets_disables_and_lists(tmp_path, monkeypatch, capsys):
    replies = iter([PASSWORD, PASSWORD, NEW_PASSWORD, NEW_PASSWORD])
    monkeypatch.setattr(manage.getpass, "getpass", lambda _: next(replies))
    command = ["--data-dir", str(tmp_path), "users"]
    assert manage.main(command + ["create", "alice"]) == 0
    store = AuthStore(tmp_path / "security.sqlite3")
    session = store.login("alice", PASSWORD)
    assert session is not None
    assert manage.main(command + ["reset", "alice"]) == 0
    assert store.get_session(session["token"]) is None
    assert store.login("alice", NEW_PASSWORD) is not None
    assert manage.main(command + ["disable", "alice"]) == 0
    assert store.login("alice", NEW_PASSWORD) is None
    assert manage.main(command + ["list"]) == 0
    output = capsys.readouterr()
    assert "alice" in output.out and "disabled" in output.out
    assert PASSWORD not in output.out + output.err
    assert NEW_PASSWORD not in output.out + output.err
    assert session["token"] not in output.out + output.err


def test_management_cli_refuses_echoed_password_input(tmp_path, monkeypatch, capsys):
    def unsafe_getpass(_):
        warnings.warn("Cannot control echo on the terminal", manage.getpass.GetPassWarning)
        pytest.fail("An echoed password read must never be attempted")

    monkeypatch.setattr(manage.getpass, "getpass", unsafe_getpass)
    assert manage.main(["--data-dir", str(tmp_path), "users", "create", "alice"]) == 1
    assert "hidden password input is required" in capsys.readouterr().err
    assert AuthStore(tmp_path / "security.sqlite3").list_users() == []


def test_management_cli_rejects_confirmation_mismatch(tmp_path, monkeypatch, capsys):
    replies = iter([PASSWORD, NEW_PASSWORD])
    monkeypatch.setattr(manage.getpass, "getpass", lambda _: next(replies))
    assert manage.main(["--data-dir", str(tmp_path), "users", "create", "alice"]) == 1
    assert "Passwords do not match" in capsys.readouterr().err
    assert AuthStore(tmp_path / "security.sqlite3").list_users() == []
