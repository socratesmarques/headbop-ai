"""Real browser smoke test; owns its server, temporary media and project storage.

Run: python scripts/browser_smoke.py
Optional HEADBOP_BROWSER_EXECUTABLE selects a Chromium executable. Otherwise use
system chromium when installed, then Playwright's installed Chromium.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "test-results"


def media_files(directory: Path) -> tuple[Path, Path]:
    image_path = directory / "character.png"
    image = Image.new("RGB", (320, 320), (31, 55, 88))
    draw = ImageDraw.Draw(image)
    draw.ellipse((90, 45, 230, 225), fill=(224, 239, 64))
    draw.ellipse((126, 104, 138, 122), fill=(18, 20, 27))
    draw.ellipse((181, 104, 193, 122), fill=(18, 20, 27))
    draw.arc((124, 125, 195, 183), 0, 180, fill=(18, 20, 27), width=6)
    image.save(image_path)
    sr, duration = 22050, 4
    samples = np.zeros(sr * duration, dtype=np.float32)
    for index, start_time in enumerate(np.arange(0.25, duration, 0.5)):
        start = round(start_time * sr)
        count = round(sr * 0.035)
        pulse = np.random.default_rng(index).normal(size=count) * np.exp(-np.arange(count) / 180)
        samples[start : start + count] = pulse * 0.4
    audio_path = directory / "beat.wav"
    sf.write(audio_path, samples, sr)
    return image_path, audio_path


def wait_server(base_url: str, server: subprocess.Popen, logfile: Path):
    for _ in range(120):
        if server.poll() is not None:
            raise RuntimeError(f"Server exited: {logfile.read_text()}")
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=1) as response:
                assert json.load(response)["ffmpeg"]
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"Server did not start: {logfile.read_text()}")


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "Horizontal overflow"
    )


def render(page):
    page.locator("#render-button").click()
    expect(page.locator("#preview-state")).to_have_text("PRONTO PARA DAR PLAY", timeout=120_000)
    expect(page.locator("#download-video")).to_have_attribute("aria-disabled", "false")
    page.wait_for_function(
        "() => document.getElementById('preview-video').readyState >= 2", timeout=30_000
    )
    page.locator("#preview-video").evaluate("video => {video.muted = true; return video.play();}")
    page.wait_for_function("() => document.getElementById('preview-video').currentTime > 0.15")
    page.locator("#preview-video").evaluate("video => video.pause()")


def main():
    RESULTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="headbop-browser-") as temporary:
        directory = Path(temporary)
        image, audio = media_files(directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment["HEADBOP_DATA_DIR"] = str(directory / "projects")
        environment["OPENBLAS_NUM_THREADS"] = "1"
        environment["OMP_NUM_THREADS"] = "1"
        logfile = RESULTS / "browser-server.log"
        with logfile.open("w") as output:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "headbop.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_server(base_url, server, logfile)
                with sync_playwright() as playwright:
                    executable = os.getenv("HEADBOP_BROWSER_EXECUTABLE") or shutil.which("chromium")
                    browser = playwright.chromium.launch(executable_path=executable, headless=True)
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1100}, reduced_motion="reduce"
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on(
                        "console",
                        lambda message: (
                            errors.append(message.text)
                            if "Content Security Policy" in message.text
                            else None
                        ),
                    )
                    try:
                        page.goto(base_url)
                        expect(page.locator("#analyze-button")).to_be_disabled()
                        no_overflow(page)
                        page.screenshot(path=str(RESULTS / "desktop-empty.png"), full_page=True)
                        page.locator("#image-file").set_input_files(image)
                        page.locator("#audio-file").set_input_files(audio)
                        page.locator("#analyze-button").click()
                        expect(page.locator("#render-button")).to_be_enabled(timeout=120_000)
                        detected = float(page.locator("#detected-bpm").inner_text())
                        assert 110 <= detected <= 130, detected
                        page.locator('[data-motion="hard"]').click()
                        page.locator("#bpm").fill("110")
                        page.locator("#intensity").fill("1.25")
                        render(page)
                        with page.expect_download() as video_download:
                            page.locator("#download-video").click()
                        download = video_download.value
                        video_path = directory / "download.mp4"
                        download.save_as(video_path)
                        video_url = page.locator("#preview-video").get_attribute("src")
                        preview_response = context.request.get(base_url + video_url)
                        assert preview_response.ok
                        assert video_path.read_bytes() == preview_response.body(), (
                            "Preview/download bytes differ"
                        )
                        page.screenshot(path=str(RESULTS / "desktop-ready.png"), full_page=True)
                        with page.expect_download() as xml_download:
                            page.locator("#download-project").click()
                        xml_path = directory / "project.xml"
                        xml_download.value.save_as(xml_path)
                        assert b"headbop-project" in xml_path.read_bytes()
                        expect(page.locator("#import-button")).to_be_enabled()
                        page.locator("#import-file").set_input_files(xml_path)
                        expect(page.locator("#notice")).to_contain_text(
                            "Projeto Headbop importado", timeout=120_000
                        )
                        expect(page.locator("#bpm")).to_have_value("110")
                        expect(page.locator('[data-motion="hard"]')).to_have_attribute(
                            "aria-pressed", "true"
                        )
                        page.locator('[data-mode="subject"]').click()
                        page.locator("#select-region").click()
                        canvas = page.locator("#image-canvas")
                        expect(canvas).to_be_visible()
                        box = canvas.bounding_box()
                        assert box
                        page.mouse.move(
                            box["x"] + box["width"] * 0.20, box["y"] + box["height"] * 0.08
                        )
                        page.mouse.down()
                        page.mouse.move(
                            box["x"] + box["width"] * 0.80, box["y"] + box["height"] * 0.80, steps=8
                        )
                        page.mouse.up()
                        page.locator("#apply-selection").click()
                        expect(page.locator("#notice")).to_contain_text(
                            "Recorte aplicado", timeout=30_000
                        )
                        page.locator('[data-motion="velocity"]').click()
                        render(page)
                        mobile = browser.new_context(
                            viewport={"width": 390, "height": 844},
                            is_mobile=True,
                            has_touch=True,
                            device_scale_factor=3,
                            reduced_motion="reduce",
                        )
                        mobile_page = mobile.new_page()
                        mobile_page.on("pageerror", lambda error: errors.append(str(error)))
                        mobile_page.goto(base_url)
                        no_overflow(mobile_page)
                        mobile_page.screenshot(
                            path=str(RESULTS / "mobile-empty.png"), full_page=True
                        )
                        mobile_page.locator("#import-file").set_input_files(xml_path)
                        expect(mobile_page.locator("#notice")).to_contain_text(
                            "Projeto Headbop importado", timeout=120_000
                        )
                        no_overflow(mobile_page)
                        mobile_page.screenshot(
                            path=str(RESULTS / "mobile-project.png"), full_page=True
                        )
                        mobile_page.locator('[data-mode="subject"]').click()
                        mobile_page.locator("#select-region").click()
                        mobile_canvas = mobile_page.locator("#image-canvas")
                        mobile_box = mobile_canvas.bounding_box()
                        assert mobile_box
                        touch = mobile.new_cdp_session(mobile_page)
                        x0 = mobile_box["x"] + mobile_box["width"] * 0.20
                        y0 = mobile_box["y"] + mobile_box["height"] * 0.08
                        x1 = mobile_box["x"] + mobile_box["width"] * 0.80
                        y1 = mobile_box["y"] + mobile_box["height"] * 0.80
                        touch.send(
                            "Input.dispatchTouchEvent",
                            {"type": "touchStart", "touchPoints": [{"x": x0, "y": y0}]},
                        )
                        for step in range(1, 9):
                            touch.send(
                                "Input.dispatchTouchEvent",
                                {
                                    "type": "touchMove",
                                    "touchPoints": [
                                        {
                                            "x": x0 + (x1 - x0) * step / 8,
                                            "y": y0 + (y1 - y0) * step / 8,
                                        }
                                    ],
                                },
                            )
                        touch.send(
                            "Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []}
                        )
                        mobile_page.locator("#apply-selection").click()
                        expect(mobile_page.locator("#notice")).to_contain_text(
                            "Recorte aplicado", timeout=30_000
                        )
                        render(mobile_page)
                        no_overflow(mobile_page)
                        mobile_page.screenshot(
                            path=str(RESULTS / "mobile-ready.png"), full_page=True
                        )
                        mobile_page.locator("#clear-project").click()
                        expect(mobile_page.locator("#notice")).to_contain_text(
                            "Arquivos do projeto removidos"
                        )
                        mobile.close()
                        page.locator("#clear-project").click()
                        expect(page.locator("#notice")).to_contain_text(
                            "Arquivos do projeto removidos"
                        )
                        assert not errors, errors
                        print(
                            "PASS: desktop upload, BPM/settings, whole MP4 playback and byte-identical download, XML export/import, manual subject selection and render, mobile layout/import/touch crop/render/delete; no JS/CSP errors"
                        )
                    except Exception:
                        page.screenshot(path=str(RESULTS / "failure.png"), full_page=True)
                        raise
                    finally:
                        context.close()
                        browser.close()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    main()
