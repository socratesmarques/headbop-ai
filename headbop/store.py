"""Private projects with immutable ownership and atomic admission reservations."""

import json
import math
import os
import re
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ID = re.compile(r"[0-9a-f]{32}\Z")
MIB = 1024 * 1024


class QuotaError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.active: set[str] = set()
        self.building: set[str] = set()
        self.reservations: dict[str, dict] = {}
        self._instance_file = None
        self.ttl = float(os.getenv("HEADBOP_TTL_HOURS", "24")) * 3600
        self.max_projects = int(os.getenv("HEADBOP_MAX_PROJECTS", "50"))
        self.user_max_projects = int(os.getenv("HEADBOP_USER_MAX_PROJECTS", "10"))
        self.max_storage = int(os.getenv("HEADBOP_MAX_STORAGE_MIB", "2048")) * MIB
        self.user_storage = int(os.getenv("HEADBOP_USER_STORAGE_MIB", "256")) * MIB
        if not math.isfinite(self.ttl) or min(self.ttl, self.max_projects, self.user_max_projects,
               self.max_storage, self.user_storage) <= 0:
            raise ValueError("Storage limits must be positive")

    @contextmanager
    def instance(self):
        """Keep one serving process per data root; reservations live in memory.

        The OS releases this advisory lock even after SIGKILL. Do not unlink its
        inode: another process may already have opened it while trying to start.
        Account management may still use the independent SQLite transactions.
        """
        with self.lock:
            if self._instance_file is not None:
                raise RuntimeError("Only one application worker may use HEADBOP_DATA_DIR")
            descriptor = os.open(self.root / ".instance.lock",
                                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            stream = os.fdopen(descriptor, "r+b", buffering=0)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise RuntimeError("Invalid application lock file")
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif os.name == "nt":
                    import msvcrt
                    if os.fstat(descriptor).st_size == 0:
                        stream.write(b"0")
                    stream.seek(0)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    raise RuntimeError("Application instance locking is unsupported on this OS")
            except OSError as exc:
                stream.close()
                raise RuntimeError("Only one application worker may use HEADBOP_DATA_DIR") from exc
            except BaseException:
                stream.close()
                raise
            self._instance_file = stream
        try:
            yield
        finally:
            with self.lock:
                stream.close()
                self._instance_file = None

    def paths(self):
        return [p for p in self.root.iterdir()
                if ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]

    def path(self, project_id: str) -> Path:
        if not ID.fullmatch(project_id):
            raise FileNotFoundError("Projeto não encontrado.")
        path = self.root / project_id
        if not path.is_dir() or path.is_symlink():
            raise FileNotFoundError("Projeto não encontrado ou expirado.")
        return path

    def owner(self, project_id: str) -> str | None:
        path = self.path(project_id) / ".owner"
        if path.is_symlink():
            raise FileNotFoundError("Projeto não encontrado.")
        try:
            owner = path.read_text().strip()
        except FileNotFoundError:
            return None
        return owner if ID.fullmatch(owner) else None

    def adopt(self, project_id: str, owner_id: str):
        """Explicit offline legacy migration; never overwrite existing ownership."""
        if not ID.fullmatch(owner_id):
            raise ValueError("Invalid owner")
        with self.lock:
            path = self.path(project_id)
            # Only complete projects can be adopted. The content remains intact.
            self.read(project_id)
            descriptor = os.open(path / ".owner", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(owner_id)

    def authorize(self, project_id, owner_id):
        if self.owner(project_id) != owner_id:
            raise FileNotFoundError("Projeto não encontrado.")

    def cleanup(self):
        with self.lock:
            for path in self.paths():
                # Existing anonymous projects are preserved for explicit adoption.
                if (self.owner(path.name) is not None
                        and path.name not in self.active | self.building
                        and time.time() - path.stat().st_mtime > self.ttl):
                    shutil.rmtree(path)

    def usage(self, owner_id=None):
        count, size = 0, 0
        for path in self.paths():
            if owner_id is not None and self.owner(path.name) != owner_id:
                continue
            count += 1
            for file in path.rglob("*"):
                try:
                    if file.is_file() and not file.is_symlink():
                        size += file.stat().st_size
                except FileNotFoundError:
                    pass  # Renderers atomically replace partial output.
        return count, size

    def reserve(self, owner_id, size, *, projects=0):
        """Called before parsing uploads or queueing native work, under one lock."""
        with self.lock:
            self.cleanup()
            global_count, global_size = self.usage()
            user_count, user_size = self.usage(owner_id)
            for reservation in self.reservations.values():
                global_count += reservation["projects"]
                global_size += reservation["size"]
                if reservation["owner"] == owner_id:
                    user_count += reservation["projects"]
                    user_size += reservation["size"]
            if global_count + projects > self.max_projects or user_count + projects > self.user_max_projects:
                raise QuotaError("quota_projects")
            if global_size + size > self.max_storage or user_size + size > self.user_storage:
                raise QuotaError("quota_storage")
            token = uuid4().hex
            self.reservations[token] = {"owner": owner_id, "size": size, "projects": projects}
            return token

    def release(self, token):
        with self.lock:
            self.reservations.pop(token, None)

    def create(self, owner_id=None, reservation=None) -> tuple[str, Path]:
        with self.lock:
            if owner_id is not None and not ID.fullmatch(owner_id):
                raise ValueError("Invalid owner")
            if reservation is None:
                reservation = self.reserve(owner_id, 0, projects=1)
                release = True
            else:
                held = self.reservations[reservation]
                if held["owner"] != owner_id or held["projects"] != 1:
                    raise ValueError("Invalid reservation")
                release = False
            try:
                project_id = uuid4().hex
                path = self.root / project_id
                path.mkdir(mode=0o700)
                if owner_id is not None:
                    (path / ".owner").write_text(owner_id)
                    (path / ".owner").chmod(0o600)
                self.reservations[reservation]["projects"] = 0
                self.building.add(project_id)
                return project_id, path
            finally:
                if release:
                    self.release(reservation)

    def read(self, project_id: str) -> dict:
        path = self.path(project_id)
        try:
            return json.loads((path / "project.json").read_text())
        except FileNotFoundError:
            raise FileNotFoundError("Projeto não encontrado.") from None

    def write(self, project: dict):
        path = self.path(project["id"])
        temporary = path / "project.json.tmp"
        temporary.write_text(json.dumps(project, allow_nan=False))
        temporary.replace(path / "project.json")
        os.utime(path, None)

    def list_owned(self, owner_id):
        with self.lock:
            result = []
            for path in self.paths():
                if self.owner(path.name) != owner_id or path.name in self.building:
                    continue
                try:
                    project = self.read(path.name)
                    result.append({key: project[key] for key in ("id", "width", "height", "duration")}
                                  | {"updated_at": path.stat().st_mtime})
                except (FileNotFoundError, KeyError, json.JSONDecodeError):
                    continue
            return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def recover(self):
        """Before accepting traffic, expose interrupted jobs as retryable failures."""
        with self.lock:
            for path in self.paths():
                if self.owner(path.name) is None:
                    continue
                try:
                    project = self.read(path.name)
                except (FileNotFoundError, json.JSONDecodeError):
                    # An interrupted upload never became a project.
                    shutil.rmtree(path)
                    continue
                if project.get("render", {}).get("status") in {"queued", "rendering"}:
                    (path / "video.mp4").unlink(missing_ok=True)
                    (path / "video.mp4.partial.mp4").unlink(missing_ok=True)
                    project["render"] = {"status": "error", "video_url": None,
                                         "error": "render_failed", "code": "render_failed"}
                    self.write(project)

    def remove(self, project_id: str):
        with self.lock:
            if project_id in self.active | self.building:
                raise ValueError("Aguarde a renderização terminar antes de excluir o projeto.")
            shutil.rmtree(self.path(project_id))
