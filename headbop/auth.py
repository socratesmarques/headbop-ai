"""Individual accounts, revocable opaque sessions and atomic persistent limits.

The database belongs to the server, never to a client-supplied path. Passwords
and bearer session tokens are never stored in plaintext. HTTP cookie, CSRF and
origin policy belong to the application layer; callers must not log credentials.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Callable
from uuid import uuid4


_SCRYPT_N = 2 ** 17
_SCRYPT_R = 8
_SCRYPT_P = 1
_HASH_SLOTS = threading.BoundedSemaphore(2)
_DUMMY_SALT = secrets.token_bytes(16)
_DUMMY_DIGEST = secrets.token_bytes(32)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}\Z")


class AuthBusyError(RuntimeError):
    """The bounded password hashing capacity is busy; reject, do not queue."""


class InvalidCredentialsError(ValueError):
    """A verified identity was revoked before its session could be issued."""


def normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("auth.invalid_username")
    normalized = username.casefold()
    if not _USERNAME.fullmatch(normalized):
        raise ValueError("auth.invalid_username")
    return normalized


def _password_bytes(password: str, *, creating: bool = False) -> bytes:
    if not isinstance(password, str) or len(password) > 128:
        raise ValueError("auth.invalid_password")
    if creating and (len(password) < 12 or not password.strip()):
        raise ValueError("auth.invalid_password")
    return password.encode("utf-8")


def _derive(password: bytes, salt: bytes) -> bytes:
    if not _HASH_SLOTS.acquire(blocking=False):
        raise AuthBusyError("auth.busy")
    try:
        # OWASP's scrypt minimum: N=2^17, r=8, p=1. Two concurrent derivations
        # consume about 256 MiB in total, independently of incoming concurrency.
        return hashlib.scrypt(password, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                              p=_SCRYPT_P, dklen=32, maxmem=256 * 1024 * 1024)
    finally:
        _HASH_SLOTS.release()


def hash_password(password: str) -> str:
    value = _password_bytes(password, creating=True)
    salt = secrets.token_bytes(16)
    digest = _derive(value, salt)
    return "$".join(("scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
                     base64.b64encode(salt).decode("ascii"),
                     base64.b64encode(digest).decode("ascii")))


def _verify_password(password: str, encoded: str | None) -> bool:
    """Unknown/invalid users still perform one bounded password derivation."""
    valid = encoded is not None
    try:
        value = _password_bytes(password)
    except (ValueError, UnicodeError):
        value, valid = b"invalid-password", False
    salt, expected = _DUMMY_SALT, _DUMMY_DIGEST
    if encoded is not None:
        try:
            algorithm, n, r, p, encoded_salt, encoded_digest = encoded.split("$")
            if (algorithm, n, r, p) != ("scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P)):
                raise ValueError("Unsupported password parameters")
            salt = base64.b64decode(encoded_salt, validate=True)
            expected = base64.b64decode(encoded_digest, validate=True)
            if len(salt) != 16 or len(expected) != 32:
                raise ValueError("Invalid password digest")
        except (ValueError, TypeError):
            salt, expected, valid = _DUMMY_SALT, _DUMMY_DIGEST, False
    actual = _derive(value, salt)
    return hmac.compare_digest(actual, expected) and valid


class AuthStore:
    def __init__(self, path: Path, *, session_ttl_seconds: float = 12 * 3600,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        if not math.isfinite(session_ttl_seconds) or not 60 <= session_ttl_seconds <= 7 * 86400:
            raise ValueError("Session lifetime must be between 60 seconds and seven days")
        self.session_ttl_seconds = float(session_ttl_seconds)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0, 1)),
                    auth_version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    password_changed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    auth_version INTEGER NOT NULL,
                    csrf TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS rate_limits (
                    bucket TEXT PRIMARY KEY,
                    count INTEGER NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rate_limit_expiry ON rate_limits(expires_at);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _user(row) -> dict:
        return {"id": row["id"], "username": row["username"], "disabled": bool(row["disabled"])}

    def create_user(self, username: str, password: str) -> dict:
        username = normalize_username(username)
        encoded = hash_password(password)
        now, user_id = self.clock(), uuid4().hex
        try:
            with self._connect() as db:
                db.execute("INSERT INTO users(id,username,password_hash,created_at,password_changed_at) VALUES(?,?,?,?,?)",
                           (user_id, username, encoded, now, now))
        except sqlite3.IntegrityError as exc:
            raise ValueError("auth.username_taken") from exc
        return {"id": user_id, "username": username, "disabled": False}

    def get_user(self, username: str) -> dict | None:
        username = normalize_username(username)
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return self._user(row) if row is not None else None

    def list_users(self) -> list[dict]:
        with self._connect() as db:
            return [self._user(row) for row in db.execute("SELECT * FROM users ORDER BY username")]

    def authenticate(self, username: str, password: str) -> dict | None:
        try:
            normalized = normalize_username(username)
        except ValueError:
            normalized = ""
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (normalized,)).fetchone()
        if not _verify_password(password, row["password_hash"] if row is not None else None):
            return None
        if row is None or row["disabled"]:
            return None
        return {"id": row["id"], "username": row["username"], "auth_version": row["auth_version"]}

    def login(self, username: str, password: str) -> dict | None:
        user = self.authenticate(username, password)
        if user is None:
            return None
        try:
            return self.create_session(user["id"], auth_version=user["auth_version"])
        except InvalidCredentialsError:
            return None

    def create_session(self, user_id: str, *, auth_version: int | None = None) -> dict:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self.clock()
        expires_at = now + self.session_ttl_seconds
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                if row is None or row["disabled"] or (auth_version is not None and row["auth_version"] != auth_version):
                    raise InvalidCredentialsError("auth.invalid_credentials")
                db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
                # Keep a bounded number of devices per identity; a new login
                # revokes the oldest session instead of growing storage forever.
                existing = db.execute("SELECT token_hash FROM sessions WHERE user_id=? ORDER BY created_at DESC, token_hash DESC",
                                      (user_id,)).fetchall()
                for old in existing[7:]:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (old["token_hash"],))
                db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                           (token_hash, user_id, row["auth_version"], csrf, now, expires_at))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"token": token, "csrf": csrf,
                "user": {"id": row["id"], "username": row["username"]}, "expires_at": expires_at}

    def get_session(self, token: str | None) -> dict | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._connect() as db:
            row = db.execute("""SELECT s.csrf,s.expires_at,u.id,u.username FROM sessions s
                                JOIN users u ON s.user_id=u.id
                                WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0
                                  AND s.auth_version=u.auth_version""",
                             (token_hash, self.clock())).fetchone()
        if row is None:
            return None
        return {"csrf": row["csrf"], "expires_at": row["expires_at"],
                "user": {"id": row["id"], "username": row["username"]}}

    def revoke_session(self, token: str | None) -> None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?",
                       (hashlib.sha256(token.encode("ascii")).hexdigest(),))

    def revoke_user_sessions(self, user_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE users SET auth_version=auth_version+1 WHERE id=?", (user_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.commit()

    def reset_password(self, username: str, password: str) -> None:
        normalized, encoded = normalize_username(username), hash_password(password)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM users WHERE username=?", (normalized,)).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("auth.user_not_found")
            db.execute("UPDATE users SET password_hash=?,password_changed_at=?,auth_version=auth_version+1 WHERE id=?",
                       (encoded, self.clock(), row["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
            db.commit()

    def disable_user(self, username: str) -> None:
        normalized = normalize_username(username)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM users WHERE username=?", (normalized,)).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("auth.user_not_found")
            db.execute("UPDATE users SET disabled=1,auth_version=auth_version+1 WHERE id=?", (row["id"],))
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
            db.commit()

    def consume_rate(self, key: str, limit: int, window_seconds: float) -> int:
        """Consume one attempt atomically; return 0 or bounded Retry-After.

        An active window is anchored to its first accepted attempt. Rejected
        requests neither extend the window nor increase its storage footprint.
        Keys are hashed so raw usernames/IPs are not retained in the rate table.
        """
        if not isinstance(key, str) or not key or len(key) > 512:
            raise ValueError("Invalid rate bucket")
        if type(limit) is not int or limit < 1 or not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("Invalid rate policy")
        now = self.clock()
        bucket = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM rate_limits WHERE expires_at<=?", (now,))
                row = db.execute("SELECT count,expires_at FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()
                if row is None:
                    db.execute("INSERT INTO rate_limits VALUES(?,?,?)", (bucket, 1, now + window_seconds))
                    retry = 0
                elif row["count"] >= limit:
                    retry = max(1, math.ceil(row["expires_at"] - now))
                else:
                    db.execute("UPDATE rate_limits SET count=count+1 WHERE bucket=?", (bucket,))
                    retry = 0
                db.commit()
                return retry
            except Exception:
                db.rollback()
                raise

    def cleanup(self) -> None:
        now = self.clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM rate_limits WHERE expires_at<=?", (now,))
            db.commit()
