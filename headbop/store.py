"""Private temporary project storage; identifiers never contain user paths."""

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.active: set[str] = set()
        self.building: set[str] = set()
        self.ttl = float(os.getenv("HEADBOP_TTL_HOURS", "24")) * 3600
        self.max_projects = int(os.getenv("HEADBOP_MAX_PROJECTS", "50"))

    def path(self, project_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise FileNotFoundError("Projeto não encontrado.")
        path = self.root / project_id
        if not path.is_dir():
            raise FileNotFoundError("Projeto não encontrado ou expirado.")
        return path

    def cleanup(self):
        with self.lock:
            for path in self.root.iterdir():
                if (path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name)
                        and path.name not in self.active | self.building
                        and time.time() - path.stat().st_mtime > self.ttl):
                    shutil.rmtree(path)

    def create(self) -> tuple[str, Path]:
        with self.lock:
            self.cleanup()
            paths = [p for p in self.root.iterdir() if p.is_dir()]
            size = 0
            for path in paths:
                for file in path.rglob("*"):
                    try:
                        if file.is_file():
                            size += file.stat().st_size
                    except FileNotFoundError:
                        # A renderer can atomically replace its temporary video.
                        continue
            if len(paths) >= self.max_projects or size >= 512 * 1024 * 1024:
                raise ValueError("Armazenamento temporário cheio. Exclua projetos e tente novamente.")
            project_id = uuid4().hex
            path = self.root / project_id
            path.mkdir(mode=0o700)
            self.building.add(project_id)
            return project_id, path

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

    def remove(self, project_id: str):
        with self.lock:
            if project_id in self.active:
                raise ValueError("Aguarde a renderização terminar antes de excluir o projeto.")
            shutil.rmtree(self.path(project_id))
