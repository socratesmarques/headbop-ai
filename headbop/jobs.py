"""Supervise complete render jobs outside the API process, including native code."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def render_timeout(value: float | None = None) -> float:
    timeout = float(os.environ.get("HEADBOP_RENDER_TIMEOUT_SECONDS", "300")
                    if value is None else value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("O timeout do render deve ser positivo e finito.")
    return timeout


def _process_group_options(isolated: bool = True) -> dict:
    if not isolated:
        return {}
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def _kill_process_tree(process: subprocess.Popen, isolated: bool = True) -> None:
    """Kill descendants even if their group leader has already exited."""
    try:
        if isolated and os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif isolated and os.name == "nt":
            # Windows does not expose POSIX killpg; /T includes descendants.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        elif process.poll() is None:
            process.kill()
    except (ProcessLookupError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    finally:
        if process.poll() is None:
            process.kill()


def _worker_command(project_dir: Path, timeout: float, max_output_bytes: int) -> list[str]:
    return [sys.executable, "-m", "headbop.worker", str(project_dir),
            str(timeout), str(max_output_bytes)]


def run_render_job(project_dir: Path, progress_callback=None,
                   timeout_seconds: float | None = None,
                   max_output_bytes: int = MAX_OUTPUT_BYTES, cancel_event=None) -> Path:
    """Return a validated MP4, or kill/reap the job and remove its output.

    The deadline includes Python imports, scene preparation, blocked encoder
    pipes, muxing, and validation. No worker/native code runs in the API thread.
    Progress describes frames submitted; the caller publishes completion only
    after this function returns successfully.
    """
    timeout = render_timeout(timeout_seconds)
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("O limite do vídeo deve ser positivo.")
    project_dir = Path(project_dir).resolve()
    output = project_dir / "video.mp4"
    partial = project_dir / "video.mp4.partial.mp4"
    deadline = time.monotonic() + timeout
    process = None
    reader = None
    complete = False
    progress = queue.Queue(maxsize=256)
    invalid_progress = threading.Event()
    # Pin imports to this checkout/package, including when the caller changes cwd.
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        package_root, environment.get("PYTHONPATH"))))

    def check_deadline():
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Render cancelado.")
        if time.monotonic() >= deadline:
            raise TimeoutError("O render excedeu o tempo limite.")

    def collect_progress():
        # Every record and the entire protocol have fixed bounds. A malicious or
        # broken worker cannot fill memory or retain a pipe reader indefinitely.
        received = 0
        try:
            while record := process.stdout.readline(513):
                received += len(record)
                if len(record) > 512 or received > 64 * 1024:
                    invalid_progress.set()
                    return
                try:
                    payload = json.loads(record)
                    done, total = payload["done"], payload["total"]
                    if (type(done) is not int or type(total) is not int
                            or not 0 <= done <= total <= 1800 or total == 0):
                        raise ValueError
                    progress.put_nowait((done, total))
                except (ValueError, TypeError, KeyError, queue.Full):
                    invalid_progress.set()
                    return
        except (OSError, ValueError):
            invalid_progress.set()

    def publish_progress():
        while True:
            try:
                done, total = progress.get_nowait()
            except queue.Empty:
                return
            if progress_callback is not None:
                progress_callback(done, total)

    try:
        check_deadline()
        process = subprocess.Popen(
            _worker_command(project_dir, timeout, max_output_bytes),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, **_process_group_options())
        reader = threading.Thread(target=collect_progress, daemon=True)
        reader.start()
        while process.poll() is None:
            check_deadline()
            if invalid_progress.is_set():
                raise RuntimeError("Progresso de render inválido.")
            publish_progress()
            # Event.wait makes shutdown responsive without busy polling.
            if cancel_event is not None:
                cancel_event.wait(min(0.05, max(0, deadline - time.monotonic())))
            else:
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        check_deadline()
        # Terminate any orphaned descendants before joining the pipe reader.
        _kill_process_tree(process)
        reader.join(timeout=min(1, max(0, deadline - time.monotonic())))
        check_deadline()
        if reader.is_alive() or invalid_progress.is_set() or process.returncode != 0:
            raise RuntimeError("Não foi possível concluir a codificação do vídeo.")
        publish_progress()
        if (not output.is_file() or output.is_symlink()
                or not 0 < output.stat().st_size <= max_output_bytes):
            raise RuntimeError("O vídeo gerado está vazio ou excede o limite de armazenamento.")
        # The worker also probes codec/streams/duration before its successful exit.
        with output.open("rb") as video:
            if video.read(12)[4:8] != b"ftyp":
                raise RuntimeError("O vídeo gerado é inválido.")
        check_deadline()
        complete = True
        return output
    finally:
        if process is not None:
            _kill_process_tree(process)
            process.wait()
            if reader is not None:
                reader.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
        partial.unlink(missing_ok=True)
        if not complete:
            output.unlink(missing_ok=True)
