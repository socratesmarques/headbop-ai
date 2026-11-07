"""Render a reproducible synthetic legacy/v2 comparison and CPU benchmark.

Run from the checkout: python scripts/render_comparison.py --output test-results/render-comparison
The fixture has known beat times and a manually selected head. It is not a
reference-video fidelity test. MP4 files and sample PNGs are kept for inspection.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headbop.animation import generate_keyframes, generate_motion  # noqa: E402
from headbop.renderer import _frame_transform, _prepare_scene, _render_frame, render_video  # noqa: E402
from headbop.rigging import build_rig  # noqa: E402


def make_fixture(directory: Path, duration: float) -> tuple[Path, Path, Path, dict, dict]:
    source = Image.new("RGBA", (640, 640))
    draw = ImageDraw.Draw(source)
    draw.rounded_rectangle((245, 280, 395, 490), 45, fill=(121, 90, 224, 255))
    draw.line((260, 320, 190, 410), fill=(121, 90, 224, 255), width=30)
    draw.line((380, 320, 450, 410), fill=(121, 90, 224, 255), width=30)
    draw.line((280, 470, 265, 570), fill=(121, 90, 224, 255), width=35)
    draw.line((360, 470, 375, 570), fill=(121, 90, 224, 255), width=35)
    draw.ellipse((210, 85, 430, 305), fill=(248, 206, 87, 255))
    draw.ellipse((265, 165, 280, 190), fill=(18, 20, 27, 255))
    draw.ellipse((360, 165, 375, 190), fill=(18, 20, 27, 255))
    draw.arc((270, 185, 370, 260), 0, 180, fill=(18, 20, 27, 255), width=7)
    image, foreground, audio = directory / "image.png", directory / "foreground.png", directory / "audio.wav"
    source.save(image)
    source.save(foreground)
    head_rect = [205 / 640, 80 / 640, 230 / 640, 230 / 640]
    rig = build_rig(image, foreground, directory,
                    {"available": True, "confidence": 1.0, "kind": "character", "head_rect": head_rect},
                    head_rect=head_rect)
    sample_rate = 22050
    samples = np.zeros(round(duration * sample_rate), dtype=np.float32)
    beats = [float(t) for t in np.arange(0.25, duration, 0.5)]
    hit_time = np.arange(round(sample_rate * 0.16)) / sample_rate
    hit = 0.7 * np.sin(2 * np.pi * 100 * hit_time) * np.exp(-hit_time * 35)
    for beat in beats:
        start = round(beat * sample_rate)
        count = min(len(hit), len(samples) - start)
        samples[start:start + count] += hit[:count]
    sf.write(audio, samples, sample_rate)
    analysis = {"bpm": 120.0, "beats": beats, "intensity": [1.0] * len(beats),
                "onsets": beats, "energy": [{"time": 0.0, "value": 1.0}, {"time": duration, "value": 1.0}],
                "silences": [], "events": [{"time": t, "kind": "low", "strength": 1.0} for t in beats],
                "rhythm_confidence": 1.0}
    return image, foreground, audio, rig, analysis


def probe(path: Path) -> dict:
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]))
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in metadata["streams"] if stream["codec_type"] == "audio")
    return {"width": video["width"], "height": video["height"], "fps": video["avg_frame_rate"],
            "frames": int(video["nb_frames"]), "video_codec": video["codec_name"],
            "audio_codec": audio["codec_name"], "pixel_format": video["pix_fmt"],
            "duration": float(video["duration"]), "bytes": path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("test-results/render-comparison"))
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--fps", type=int, choices=(24, 30, 60), default=60)
    parser.add_argument("--resolution", type=int, choices=(640, 720, 1080), default=1080)
    parser.add_argument("--aspect", choices=("source", "square", "vertical"), default="vertical")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or not 0.5 <= args.duration <= 30:
        parser.error("duration must be between 0.5 and 30 seconds")
    args.output.mkdir(parents=True, exist_ok=True)
    image, foreground, audio, rig, analysis = make_fixture(args.output, args.duration)
    selected = {"engine": "v2", "fps": args.fps, "resolution": args.resolution, "aspect": args.aspect,
                "bpm": 120.0, "intensity": 1.0, "motion": "auto", "speed": 1.0,
                "easing": "natural", "squash": 0.25}
    timelines = {
        "legacy": generate_keyframes(args.duration, analysis["beats"], analysis["intensity"], 120, 1, "velocity"),
        "v2": generate_motion(args.duration, analysis, selected, rig),
    }
    report = {"fixture": "synthetic character; manual head rectangle; known 120 BPM beat timestamps",
              "reference_available": False, "duration_seconds": args.duration, "rig_kind": rig["kind"],
              "v2_settings": selected, "renders": {}}
    for name, timeline in timelines.items():
        settings = selected if name == "v2" else None
        output = args.output / f"{name}.mp4"
        started = time.perf_counter()
        render_video(image, foreground, audio, output, timeline, args.duration, "subject",
                     settings=settings, rig=rig)
        elapsed = time.perf_counter() - started
        metadata = probe(output)
        report["renders"][name] = {**metadata, "elapsed_seconds": round(elapsed, 3),
                                    "encoded_frames_per_second": round(metadata["frames"] / elapsed, 2)}
        scene = _prepare_scene(image, foreground, "subject", settings, rig)
        for label, timestamp in (("anticipation", 0.20), ("impact", 0.25), ("recovery", 0.38)):
            pixels = _render_frame(scene, _frame_transform(timeline, timestamp))
            Image.fromarray(pixels).save(args.output / f"{name}-{label}.png")
    from PIL import ImageDraw
    sheet = Image.new("RGB", (960, 1100), (18, 20, 27))
    draw = ImageDraw.Draw(sheet)
    for row, name in enumerate(("legacy", "v2")):
        draw.text((12, row * 550 + 8), f"{name} — generated fixture, not an external reference", fill="white")
        for column, label in enumerate(("anticipation", "impact", "recovery")):
            with Image.open(args.output / f"{name}-{label}.png") as tile:
                tile.thumbnail((310, 490), Image.Resampling.LANCZOS)
                sheet.paste(tile, (column * 320 + (320 - tile.width) // 2, row * 550 + 30))
            draw.text((column * 320 + 15, row * 550 + 525), label, fill="white")
    sheet.save(args.output / "comparison.png")
    try:
        import resource
        report["python_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["memory_note"] = "Python process peak on Linux; excludes FFmpeg child and is not a hard memory limit"
    except ImportError:
        pass
    (args.output / "metrics.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
