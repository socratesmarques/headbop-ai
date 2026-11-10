"""Private subprocess entry point for one server-owned render project."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys

from .renderer import render_video
from .settings import effective_settings


def _limit_output(max_output_bytes: int) -> None:
    if os.name == "posix":
        import resource

        # Inherited by FFmpeg: even while blocked in native code it cannot grow
        # its MP4 beyond the reservation made by the API's admission controller.
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_output_bytes, max_output_bytes))


def _validate_video(output: Path, max_output_bytes: int, timeout: float) -> None:
    if not output.is_file() or not 0 < output.stat().st_size <= max_output_bytes:
        raise RuntimeError("Vídeo vazio ou maior que o limite permitido.")
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_name,codec_type,nb_frames:format=duration", "-of", "json", str(output),
    ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=timeout, check=True)
    metadata = json.loads(probe.stdout)
    streams = metadata.get("streams", [])
    duration = float(metadata.get("format", {}).get("duration", 0))
    if (not math.isfinite(duration) or duration <= 0
            or not any(stream.get("codec_type") == "video" and stream.get("codec_name") == "h264"
                       and int(stream.get("nb_frames", 0)) > 0 for stream in streams)
            or not any(stream.get("codec_type") == "audio" and stream.get("codec_name") == "aac"
                       for stream in streams)):
        raise RuntimeError("O vídeo gerado não possui fluxos H.264/AAC válidos.")


def main() -> int:
    try:
        project_dir = Path(sys.argv[1])
        timeout, max_output_bytes = float(sys.argv[2]), int(sys.argv[3])
        _limit_output(max_output_bytes)
        project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        settings = effective_settings(project["settings"])

        def progress(done, total):
            print(json.dumps({"done": done, "total": total}, separators=(",", ":")), flush=True)

        output = project_dir / "video.mp4"
        render_video(project_dir / "image.png",
                     project_dir / "foreground.png" if project["segmentation"]["available"] else None,
                     project_dir / "audio.wav", output, project["keyframes"], project["duration"],
                     settings["mode"], settings=settings, rig=project.get("rig"),
                     progress_callback=progress, timeout_seconds=timeout,
                     isolate_process_group=False)
        _validate_video(output, max_output_bytes, timeout)
        return 0
    except Exception:
        # Parent reports a localized error; no paths or unbounded native logs are
        # exposed to users or retained on disk. The supervisor owns cleanup.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
