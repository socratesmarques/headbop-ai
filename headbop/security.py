"""HTTP policy and bounded admission, independent of media processing."""

import hmac
import math
import os
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException

COOKIE = "headbop_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


def fail(status, code, **params):
    headers = {"Retry-After": str(params.get("seconds", 1))} if status in {429, 503} else None
    raise HTTPException(status, {"code": code, "params": params}, headers=headers)


@dataclass
class SecurityConfig:
    environment: str
    origin: str
    render_timeout: float
    video_bytes: int

    @classmethod
    def from_env(cls):
        return cls(os.getenv("HEADBOP_ENV", "production"),
                   os.getenv("HEADBOP_PUBLIC_ORIGIN", "").rstrip("/"),
                   float(os.getenv("HEADBOP_RENDER_TIMEOUT_SECONDS", "300")),
                   int(os.getenv("HEADBOP_MAX_VIDEO_MIB", "64")) * 1024 * 1024)

    @property
    def secure_cookie(self):
        return self.environment == "production" or self.origin.startswith("https://")

    def validate(self):
        if self.environment not in {"production", "development"}:
            raise ValueError("HEADBOP_ENV must be production or development")
        if self.environment == "production" and not self.origin:
            raise ValueError("Production requires HEADBOP_PUBLIC_ORIGIN=https://your-domain")
        if self.origin:
            parsed = urlsplit(self.origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.path or parsed.query or parsed.fragment or parsed.username
                    or parsed.password or (self.environment == "production" and parsed.scheme != "https")):
                raise ValueError("HEADBOP_PUBLIC_ORIGIN must be one canonical HTTPS origin")
            # Check malformed/non-numeric ports too.
            _ = parsed.port
        if not math.isfinite(self.render_timeout) or not 0 < self.render_timeout <= 1800:
            raise ValueError("Render timeout must be positive and at most 1800 seconds")
        if not 1 <= self.video_bytes <= 1024 * 1024 * 1024:
            raise ValueError("Video limit must be between 1 byte and 1 GiB")

    def request_origin(self, request):
        origin = f"{request.url.scheme}://{request.url.netloc}"
        if self.origin:
            # Host must match configured origin. Scheme can be set by a trusted
            # reverse proxy; authentication itself always uses configured origin.
            if request.url.netloc != urlsplit(self.origin).netloc:
                fail(403, "origin_forbidden")
            return self.origin
        if request.url.hostname not in LOCAL_HOSTS:
            fail(403, "origin_forbidden")
        return origin

    def check_origin(self, request):
        expected = self.request_origin(request)
        if request.headers.get("origin") != expected:
            fail(403, "origin_forbidden")


def check_csrf(request, session):
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8", errors="replace"),
                                               session["csrf"].encode("ascii")):
        fail(403, "csrf_failed")


class Admission:
    """One analysis per user and two globally, admitted before body parsing."""
    def __init__(self):
        self.lock = threading.Lock()
        self.users = set()

    def enter(self, user_id):
        with self.lock:
            if user_id in self.users or len(self.users) >= 2:
                fail(429, "capacity_busy", seconds=2)
            self.users.add(user_id)

    def leave(self, user_id):
        with self.lock:
            self.users.discard(user_id)
