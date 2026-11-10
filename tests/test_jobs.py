import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image
import pytest
import soundfile as sf

from headbop import jobs


@pytest.fixture
def project(tmp_path):
    Image.new("RGB", (160, 120), "red").save(tmp_path / "image.png")
    sf.write(tmp_path / "audio.wav", 0.1 * np.sin(
        2 * np.pi * 440 * np.arange(11025) / 22050), 22050)
    metadata = {
        "duration": 0.5, "segmentation": {"available": False},
        "settings": {"bpm": 120, "motion": "smooth", "mode": "whole"},
        "keyframes": [{"time": value, "x": 0, "y": 0, "rotation": 0, "scale": 1}
                      for value in (0, 0.5)],
    }
    (tmp_path / "project.json").write_text(json.dumps(metadata))
    return tmp_path


def replace_worker(monkeypatch, source):
    monkeypatch.setattr(jobs, "_worker_command", lambda path, *_: [
        sys.executable, "-c", source, str(path)])


def assert_no_outputs(project):
    assert not (project / "video.mp4").exists()
    assert not (project / "video.mp4.partial.mp4").exists()


def process_is_running(pid):
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().split(") ", 1)[1].split()[0] != "Z"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native-call/group regression")
def test_supervisor_times_out_native_worker_and_terminates_descendants(project, monkeypatch):
    replace_worker(monkeypatch, """
import ctypes, pathlib, subprocess, sys
path = pathlib.Path(sys.argv[1])
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
(path / 'child.pid').write_text(str(child.pid))
(path / 'video.mp4.partial.mp4').write_bytes(b'partial')
(path / 'video.mp4').write_bytes(b'unvalidated')
ctypes.CDLL(None).sleep(60)
""")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        jobs.run_render_job(project, timeout_seconds=0.5)
    assert time.monotonic() - started < 3
    assert not process_is_running(int((project / "child.pid").read_text()))
    assert_no_outputs(project)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group regression")
def test_descendants_are_killed_when_worker_fails_first(project, monkeypatch):
    replace_worker(monkeypatch, """
import pathlib, subprocess, sys
path = pathlib.Path(sys.argv[1])
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
(path / 'child.pid').write_text(str(child.pid))
sys.exit(1)
""")
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        jobs.run_render_job(project, timeout_seconds=3)
    assert time.monotonic() - started < 2
    assert not process_is_running(int((project / "child.pid").read_text()))
    assert_no_outputs(project)


def test_shutdown_cancels_and_reaps_worker(project, monkeypatch):
    replace_worker(monkeypatch, "import time; time.sleep(60)")
    original_popen = subprocess.Popen
    processes = []

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="cancelado"):
            jobs.run_render_job(project, timeout_seconds=5, cancel_event=cancel)
    finally:
        timer.cancel()
        timer.join()
    assert time.monotonic() - started < 2
    assert len(processes) == 1 and processes[0].returncode is not None
    assert processes[0].stdout.closed
    assert_no_outputs(project)


@pytest.mark.parametrize("payload", [
    "'x' * 100_000", "'{\"done\":1,\"total\":0}\\n'", "'not json\\n'",
])
def test_unbounded_or_invalid_progress_fails_without_retaining_output(project, monkeypatch, payload):
    replace_worker(monkeypatch, f"import sys, time; sys.stdout.write({payload}); "
                   "sys.stdout.flush(); time.sleep(60)")
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        jobs.run_render_job(project, timeout_seconds=3)
    assert time.monotonic() - started < 2
    assert_no_outputs(project)


def test_empty_success_cannot_publish_a_ready_video(project, monkeypatch):
    replace_worker(monkeypatch, "print('{\"done\":12,\"total\":12}')")
    with pytest.raises(RuntimeError):
        jobs.run_render_job(project, timeout_seconds=3)
    assert_no_outputs(project)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="FFmpeg required")
def test_real_worker_renders_and_validates_media_after_a_failed_job(project, monkeypatch):
    with monkeypatch.context() as failed:
        replace_worker(failed, "import time; time.sleep(60)")
        with pytest.raises(TimeoutError):
            jobs.run_render_job(project, timeout_seconds=0.2)
    updates = []
    output = jobs.run_render_job(project, lambda done, total: updates.append((done, total)),
                                 timeout_seconds=15)
    assert output == project / "video.mp4" and output.stat().st_size > 0
    assert updates and updates[-1] == (12, 12)
    assert updates == sorted(updates) and len(set(updates)) == len(updates)
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]))
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    assert video["codec_name"] == "h264" and int(video["nb_frames"]) == 12
    assert any(stream["codec_name"] == "aac" for stream in metadata["streams"])
    assert not (project / "video.mp4.partial.mp4").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable and RLIMIT_FSIZE")
def test_worker_output_limit_stops_encoder_and_cleans_files(project, monkeypatch):
    encoder = project / "ffmpeg"
    encoder.write_text(f"#!{sys.executable}\nimport pathlib, sys\n"
                       "sys.stdin.buffer.read()\n"
                       "pathlib.Path(sys.argv[-1]).write_bytes(b'x' * 8192)\n")
    encoder.chmod(0o755)
    monkeypatch.setenv("PATH", str(project) + os.pathsep + os.environ["PATH"])
    with pytest.raises(RuntimeError):
        jobs.run_render_job(project, timeout_seconds=15, max_output_bytes=1024)
    assert_no_outputs(project)


@pytest.mark.skipif(os.name != "posix", reason="POSIX fake encoder")
def test_worker_rejects_nonempty_but_invalid_mp4(project, monkeypatch):
    encoder = project / "ffmpeg"
    encoder.write_text(f"#!{sys.executable}\nimport pathlib, sys\n"
                       "sys.stdin.buffer.read()\n"
                       "pathlib.Path(sys.argv[-1]).write_bytes(b'not an MP4')\n")
    encoder.chmod(0o755)
    monkeypatch.setenv("PATH", str(project) + os.pathsep + os.environ["PATH"])
    with pytest.raises(RuntimeError):
        jobs.run_render_job(project, timeout_seconds=15)
    assert_no_outputs(project)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_deadlines_fail_before_starting_a_process(project, monkeypatch, timeout):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("process started"))
    with pytest.raises(ValueError):
        jobs.run_render_job(project, timeout_seconds=timeout)


def test_default_deadline_uses_server_configuration(monkeypatch):
    monkeypatch.setenv("HEADBOP_RENDER_TIMEOUT_SECONDS", "0.4")
    assert jobs.render_timeout() == 0.4


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("ffmpeg") is None,
                    reason="Linux process-group regression with real FFmpeg")
def test_deadline_includes_probe_after_encoder_has_finished(project, monkeypatch):
    probe = project / "ffprobe"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[-1]).with_name('probe.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n")
    probe.chmod(0o755)
    monkeypatch.setenv("PATH", str(project) + os.pathsep + os.environ["PATH"])
    updates = []
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        jobs.run_render_job(project, lambda done, total: updates.append((done, total)),
                            timeout_seconds=3)
    assert time.monotonic() - started < 5
    # All frames reached the encoder, but probing never completed: no ready file.
    assert updates and updates[-1] == (12, 12)
    assert not process_is_running(int((project / "probe.pid").read_text()))
    assert_no_outputs(project)


def test_supervisor_callback_failure_reaps_worker_and_cleans_output(project, monkeypatch):
    replace_worker(monkeypatch, """
import pathlib, sys, time
path = pathlib.Path(sys.argv[1])
(path / 'video.mp4.partial.mp4').write_bytes(b'partial')
print('{"done":1,"total":12}', flush=True)
time.sleep(60)
""")
    original_popen = subprocess.Popen
    processes = []

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def reject_progress(done, total):
        raise RuntimeError("callback failed")

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    with pytest.raises(RuntimeError, match="callback failed"):
        jobs.run_render_job(project, reject_progress, timeout_seconds=3)
    assert len(processes) == 1 and processes[0].returncode is not None
    assert processes[0].stdout.closed
    assert_no_outputs(project)
