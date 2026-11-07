"""Real browser smoke test; owns its server, temporary media and project storage.

Run: python scripts/browser_smoke.py
Optional HEADBOP_BROWSER_EXECUTABLE selects a Chromium executable. Otherwise use
system chromium when installed, then Playwright's installed Chromium. Screenshots
and the server log are written to test-results/.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from xml.etree import ElementTree as ET

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
    draw.rounded_rectangle((104, 158, 216, 285), radius=26, fill=(51, 194, 190))
    draw.ellipse((90, 40, 230, 198), fill=(224, 239, 64))
    draw.ellipse((126, 94, 138, 112), fill=(18, 20, 27))
    draw.ellipse((181, 94, 193, 112), fill=(18, 20, 27))
    draw.arc((124, 115, 195, 173), 0, 180, fill=(18, 20, 27), width=6)
    image.save(image_path)
    sr, duration = 22050, 4
    samples = np.zeros(sr * duration, dtype=np.float32)
    for index, start_time in enumerate(np.arange(0.25, duration, 0.5)):
        start = round(start_time * sr)
        count = round(sr * 0.035)
        pulse = np.random.default_rng(index).normal(size=count) * np.exp(-np.arange(count) / 180)
        samples[start : start + count] = pulse * 0.4
    audio_path = directory / "beat.wav"
    sf.write(audio_path, samples, sr, subtype="PCM_16")
    return image_path, audio_path


def legacy_fixture(directory: Path, image: Path, audio: Path) -> Path:
    """Produce the original public schema without depending on the new exporter."""
    root = ET.Element("headbop-project", {"version": "1"})
    ET.SubElement(root, "settings", {
        "mode": "whole", "motion": "hard", "bpm": "110", "intensity": "0.8"
    })
    assets = ET.SubElement(root, "assets")
    for kind, mime, path in (("image", "image/png", image), ("audio", "audio/wav", audio)):
        item = ET.SubElement(assets, kind, {"encoding": "base64", "mime": mime})
        item.text = base64.b64encode(path.read_bytes()).decode("ascii")
    timeline = ET.SubElement(root, "timeline", {"fps": "24", "duration": "4"})
    for timestamp, y in ((0, 0), (2, -0.025), (4, 0)):
        ET.SubElement(timeline, "keyframe", {
            "time": str(timestamp), "x": "0", "y": str(y), "rotation": "0", "scale": "1"
        })
    path = directory / "legacy-v1.xml"
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return path


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


def watch_errors(page, errors):
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: (
        errors.append(message.text) if "Content Security Policy" in message.text else None
    ))


def ready(page, *, generate=True):
    if generate:
        page.locator("#render-button").click()
    expect(page.locator("#preview-state")).to_have_text("PRONTO PARA DAR PLAY", timeout=120_000)
    expect(page.locator("#download-video")).to_have_attribute("aria-disabled", "false")
    page.wait_for_function(
        "() => document.getElementById('preview-video').readyState >= 2", timeout=30_000
    )
    page.locator("#preview-video").evaluate("video => {video.muted = true; return video.play();}")
    page.wait_for_function("() => document.getElementById('preview-video').currentTime > 0.15")
    page.locator("#preview-video").evaluate("video => video.pause()")


def project_data(page):
    url = page.locator("#download-project").get_attribute("href")
    assert url and url.endswith("/export"), url
    response = page.request.get(urllib.parse.urljoin(page.url, url[:-len("/export")]))
    assert response.ok, response.text()
    return response.json()


def import_project(page, path):
    with page.expect_response(
        lambda response: response.url.endswith("/api/import") and response.request.method == "POST",
        timeout=120_000,
    ) as response:
        page.locator("#import-file").set_input_files(path)
    assert response.value.ok, response.value.text()
    expect(page.locator("#notice")).to_contain_text("Projeto Headbop importado", timeout=120_000)
    expect(page.locator("#import-button")).to_be_enabled()


def export_xml(page, path, version):
    with page.expect_download() as download:
        page.locator("#download-project").click()
    download.value.save_as(path)
    root = ET.fromstring(path.read_bytes())
    assert root.tag == "headbop-project" and root.attrib["version"] == str(version)
    expect(page.locator("#import-button")).to_be_enabled()
    return root


def download_video(page, path):
    with page.expect_download() as download:
        page.locator("#download-video").click()
    download.value.save_as(path)
    video_url = page.locator("#preview-video").get_attribute("src")
    response = page.request.get(urllib.parse.urljoin(page.url, video_url))
    assert response.ok
    assert path.read_bytes() == response.body(), "Preview/download bytes differ"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    assert any(stream["codec_type"] == "audio" for stream in streams), "MP4 has no audio"
    return next(stream for stream in streams if stream["codec_type"] == "video")


def select_rectangle(page, trigger, rect, *, touch_context=None):
    page.locator(trigger).click()
    canvas = page.locator("#image-canvas")
    expect(canvas).to_be_visible()
    canvas.scroll_into_view_if_needed()
    box = canvas.bounding_box()
    assert box
    x, y, width, height = rect
    x0, y0 = box["x"] + box["width"] * x, box["y"] + box["height"] * y
    x1, y1 = x0 + box["width"] * width, y0 + box["height"] * height
    if touch_context:
        touch = touch_context.new_cdp_session(page)
        touch.send("Input.dispatchTouchEvent", {
            "type": "touchStart", "touchPoints": [{"x": x0, "y": y0}]
        })
        for step in range(1, 9):
            touch.send("Input.dispatchTouchEvent", {
                "type": "touchMove", "touchPoints": [{
                    "x": x0 + (x1 - x0) * step / 8,
                    "y": y0 + (y1 - y0) * step / 8,
                }],
            })
        touch.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        touch.detach()
    else:
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x1, y1, steps=8)
        page.mouse.up()
    endpoint = "/rig" if trigger == "#select-head" else "/segment"
    with page.expect_response(
        lambda response: response.url.endswith(endpoint) and response.request.method == "POST"
    ) as response:
        page.locator("#apply-selection").click()
    assert response.value.ok, response.value.text()
    expect(page.locator("#selection-toolbar")).to_be_hidden()
    expect(page.locator("#render-button")).to_be_enabled()
    expect(page.locator("#download-video")).to_have_attribute("aria-disabled", "true")
    data = response.value.json()
    if endpoint == "/rig":
        for actual, expected in zip(data["rig"]["head_rect"], rect, strict=True):
            assert abs(actual - expected) < 0.015, data["rig"]
    return data


def advanced(page):
    details = page.locator("#advanced-settings")
    if not details.evaluate("element => element.open"):
        details.locator("summary").click()


def main():
    RESULTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="headbop-browser-") as temporary:
        directory = Path(temporary)
        image, audio = media_files(directory)
        legacy = legacy_fixture(directory, image, audio)
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
                [sys.executable, "-m", "uvicorn", "headbop.main:app", "--host", "127.0.0.1",
                 "--port", str(port)],
                cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT,
            )
            try:
                wait_server(base_url, server, logfile)
                with sync_playwright() as playwright:
                    selected_browser = os.getenv("HEADBOP_BROWSER_EXECUTABLE")
                    executable = None if selected_browser == "playwright" else (selected_browser or shutil.which("chromium"))
                    browser = playwright.chromium.launch(executable_path=executable, headless=True)
                    print(f"Browser: {executable or 'Playwright bundled headless'}, version: {browser.version}", flush=True)
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1100}, reduced_motion="reduce"
                    )
                    page = context.new_page()
                    errors = []
                    watch_errors(page, errors)
                    mobile = None
                    try:
                        page.goto(base_url)
                        expect(page.locator("#analyze-button")).to_be_disabled()
                        assert page.locator('[data-motion="reference"]').count() == 0
                        no_overflow(page)
                        page.screenshot(path=str(RESULTS / "desktop-empty.png"), full_page=True)
                        page.locator("#image-file").set_input_files(image)
                        page.locator("#audio-file").set_input_files(audio)
                        page.locator("#analyze-button").click()
                        ready(page, generate=False)  # New uploads render without another click.
                        expect(page.locator('[data-motion="auto"]')).to_have_attribute(
                            "aria-pressed", "true"
                        )
                        expect(page.locator("#engine")).to_have_value("v2")
                        expect(page.locator("#fps")).to_have_value("24")
                        expect(page.locator("#resolution")).to_have_value("640")
                        expect(page.locator("#aspect")).to_have_value("source")
                        detected = float(page.locator("#detected-bpm").inner_text())
                        assert 110 <= detected <= 130, detected
                        initial = project_data(page)
                        assert initial["analysis"]["energy"] and initial["analysis"]["onsets"]
                        assert initial["capabilities"]["reference_available"] is False
                        assert initial["mask_url"] and initial["rig"]
                        original_video = page.locator("#preview-video").get_attribute("src")
                        for view in ("source", "mask", "rig"):
                            page.locator(f'[data-view="{view}"]').click()
                            expect(page.locator("#image-canvas")).to_be_visible()
                            expect(page.locator("#download-video")).to_have_attribute("aria-disabled", "false")
                            assert page.locator("#preview-video").get_attribute("src") == original_video
                        page.screenshot(path=str(RESULTS / "desktop-rig.png"), full_page=True)
                        page.locator('[data-view="video"]').click()
                        expect(page.locator("#preview-video")).to_be_visible()
                        advanced(page)
                        page.locator("#engine").select_option("legacy")
                        expect(page.locator("#engine")).to_have_value("legacy")
                        page.locator("#engine").select_option("v2")
                        page.locator('[data-motion="hard"]').click()
                        page.locator("#bpm").fill("110")
                        page.locator("#intensity").fill("1.25")
                        for control, value in (("fps", "30"), ("resolution", "720"),
                                               ("aspect", "vertical"), ("easing", "ease-in-out")):
                            page.locator(f"#{control}").select_option(value)
                        page.locator("#speed").fill("1.25")
                        page.locator("#squash").fill("0.4")
                        page.locator('[data-mode="subject"]').click()
                        select_rectangle(page, "#select-region", [0.20, 0.08, 0.60, 0.84])
                        select_rectangle(page, "#select-head", [0.27, 0.12, 0.46, 0.49])
                        ready(page)
                        page.locator("#timeline-seek").fill("2")
                        page.wait_for_function(
                            "() => Math.abs(document.getElementById('preview-video').currentTime - 2) < 0.15"
                        )
                        expect(page.locator("#playhead-label")).to_contain_text("00:02")
                        stream = download_video(page, directory / "v2.mp4")
                        assert stream["avg_frame_rate"] == "30/1", stream
                        assert (stream["width"], stream["height"]) == (720, 1280), stream
                        before_export = project_data(page)
                        assert before_export["settings"]["speed"] == 1.25
                        assert before_export["settings"]["squash"] == 0.4
                        assert before_export["settings"]["easing"] == "ease-in-out"
                        no_overflow(page)
                        page.screenshot(path=str(RESULTS / "desktop-ready.png"), full_page=True)
                        xml_path = directory / "project-v2.xml"
                        export_xml(page, xml_path, 2)
                        import_project(page, xml_path)
                        expect(page.locator("#engine")).to_have_value("v2")
                        expect(page.locator("#bpm")).to_have_value("110")
                        expect(page.locator("#fps")).to_have_value("30")
                        expect(page.locator("#resolution")).to_have_value("720")
                        expect(page.locator("#aspect")).to_have_value("vertical")
                        imported = project_data(page)
                        assert imported["keyframes"] == before_export["keyframes"]
                        assert imported["rig"]["head_rect"] == before_export["rig"]["head_rect"]

                        mobile = browser.new_context(
                            viewport={"width": 390, "height": 844}, is_mobile=True,
                            has_touch=True, device_scale_factor=3, reduced_motion="reduce",
                        )
                        mobile_page = mobile.new_page()
                        watch_errors(mobile_page, errors)
                        mobile_page.goto(base_url)
                        no_overflow(mobile_page)
                        mobile_page.screenshot(path=str(RESULTS / "mobile-empty.png"), full_page=True)
                        import_project(mobile_page, xml_path)
                        advanced(mobile_page)
                        no_overflow(mobile_page)
                        mobile_page.screenshot(path=str(RESULTS / "mobile-project.png"), full_page=True)
                        select_rectangle(mobile_page, "#select-region", [0.20, 0.08, 0.60, 0.84],
                                         touch_context=mobile)
                        select_rectangle(mobile_page, "#select-head", [0.27, 0.12, 0.46, 0.49],
                                         touch_context=mobile)
                        ready(mobile_page)
                        no_overflow(mobile_page)
                        mobile_page.screenshot(path=str(RESULTS / "mobile-ready.png"), full_page=True)
                        mobile_page.locator("#clear-project").click()
                        expect(mobile_page.locator("#notice")).to_contain_text("Arquivos do projeto removidos")

                        import_project(page, legacy)
                        expect(page.locator("#engine")).to_have_value("legacy")
                        expect(page.locator('[data-motion="hard"]')).to_have_attribute("aria-pressed", "true")
                        assert len(project_data(page)["keyframes"]) == 3
                        ready(page)
                        legacy_stream = download_video(page, directory / "legacy.mp4")
                        assert legacy_stream["avg_frame_rate"] == "24/1", legacy_stream
                        export_xml(page, directory / "legacy-roundtrip.xml", 1)
                        page.screenshot(path=str(RESULTS / "desktop-legacy.png"), full_page=True)
                        page.locator("#clear-project").click()
                        expect(page.locator("#notice")).to_contain_text("Arquivos do projeto removidos")
                        assert not errors, errors
                        print(
                            "PASS: auto v2 preview, energy/onsets, mask/rig views, engine and advanced settings, "
                            "manual crop/head, timeline seek, 720p/30fps vertical MP4 with audio and identical "
                            "download, XML v2 frame/rig roundtrip, legacy XML v1 import/render/export, mobile "
                            "layout/touch crop/head/render/delete; no JS/CSP errors"
                        )
                    except Exception:
                        diagnostic = page.evaluate("""() => {
                            const video = document.getElementById('preview-video');
                            return {notice: document.getElementById('notice')?.textContent,
                              video: video && {error: video.error && {code:video.error.code, message:video.error.message},
                                readyState:video.readyState, networkState:video.networkState,
                                duration:video.duration, currentTime:video.currentTime,
                                h264aac:video.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"')}};
                        }""")
                        print("Browser failure diagnostic:", json.dumps(diagnostic), "JS/CSP:", errors, flush=True)
                        page.screenshot(path=str(RESULTS / "failure.png"), full_page=True)
                        if mobile and mobile.pages:
                            mobile.pages[0].screenshot(path=str(RESULTS / "mobile-failure.png"), full_page=True)
                        raise
                    finally:
                        if mobile:
                            mobile.close()
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
