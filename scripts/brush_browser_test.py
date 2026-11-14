"""Browser regression for selecting movement with a bounded smart brush.

Uses real pointer/touch/keyboard events with deterministic API fixtures. Actual
segmentation and video generation are exercised by painting/API tests and smoke.
"""

from copy import deepcopy
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from i18n_browser_test import Handler, IMAGE, PROJECT

ROOT = Path(__file__).resolve().parents[1]


def draw_stroke(page, start=(0.25, 0.35), end=(0.55, 0.55), *, touch=None):
    canvas = page.locator('#image-canvas')
    canvas.scroll_into_view_if_needed()
    box = canvas.evaluate('''canvas => {
      const bounds = canvas.getBoundingClientRect();
      const scale = Math.min(bounds.width / canvas.width, bounds.height / canvas.height);
      const width = canvas.width * scale, height = canvas.height * scale;
      return {x:bounds.left+(bounds.width-width)/2, y:bounds.top+(bounds.height-height)/2, width, height};
    }''')
    assert box and box['width'] > 0 and box['height'] > 0
    first = {'x': box['x'] + start[0] * box['width'], 'y': box['y'] + start[1] * box['height']}
    last = {'x': box['x'] + end[0] * box['width'], 'y': box['y'] + end[1] * box['height']}
    if touch:
        # CDP touch coordinates are relative to the visual viewport, whereas
        # DOM bounds and PointerEvent.clientX/Y use the layout viewport. Mobile
        # browsers can pan the visual viewport without changing those bounds.
        viewport = page.evaluate('({x: visualViewport.offsetLeft, y: visualViewport.offsetTop})')
        first = {'x': first['x'] - viewport['x'], 'y': first['y'] - viewport['y']}
        last = {'x': last['x'] - viewport['x'], 'y': last['y'] - viewport['y']}
        cdp = touch.new_cdp_session(page)
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [first]})
        for step in range(1, 9):
            cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{
                'x': first['x'] + (last['x'] - first['x']) * step / 8,
                'y': first['y'] + (last['y'] - first['y']) * step / 8,
            }]})
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        cdp.detach()
    else:
        page.mouse.move(first['x'], first['y'])
        page.mouse.down()
        page.mouse.move(last['x'], last['y'], steps=8)
        page.mouse.up()


def run_case(browser, base_url, *, mobile=False):
    context = browser.new_context(base_url=base_url, viewport={
        'width': 390 if mobile else 1440, 'height': 844 if mobile else 1100,
    }, is_mobile=mobile, has_touch=mobile, reduced_motion='reduce', locale='pt-BR')
    page = context.new_page()
    requests = []
    errors = []
    failure = {'paint': False}
    project = deepcopy(PROJECT)
    project['rig'] = {'kind': 'single', 'available': True, 'pivots': {'body': [.5, .5], 'head': [.5, .5]}}

    def api(route):
        path = urlsplit(route.request.url).path
        method = route.request.method
        requests.append((method, path, route.request.post_data_json if route.request.headers.get('content-type', '').startswith('application/json') else None))
        status = 200
        if path in {'/api/auth/session', '/api/auth/guest'}:
            body = {'user': {'id': 'b' * 32, 'username': 'visitor', 'guest': True},
                    'csrf': 'test-csrf', 'expires_at': time.time() + 3600}
        elif path == '/api/projects' and method == 'GET':
            body = {'projects': []}
        elif path.endswith('/paint'):
            assert route.request.headers.get('x-csrf-token') == 'test-csrf'
            if failure['paint']:
                body, status = {'code': 'capacity_busy'}, 429
            else:
                project['settings']['mode'] = 'subject'
                body = project
        elif path.endswith('/render'):
            body = {'status': 'rendering', 'progress': 5, 'timeout_seconds': 630}
        else:
            body = project
        route.fulfill(status=status, content_type='application/json', body=json.dumps(body))

    page.route('**/api/**', api)
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(base_url + '#studio')
        expect(page.locator('#studio')).to_be_visible()
        page.locator('#image-file').set_input_files({'name': 'character.png', 'mimeType': 'image/png', 'buffer': IMAGE.getvalue()})
        page.locator('#audio-file').set_input_files({'name': 'beat.wav', 'mimeType': 'audio/wav', 'buffer': b'fixture-audio'})
        page.locator('#analyze-button').click()
        expect(page.locator('#brush-panel')).to_be_visible()
        expect(page.locator('#render-button')).to_be_disabled()
        expect(page.locator('#select-head')).to_be_hidden()
        assert not any(path.endswith('/render') for _, path, _ in requests)
        assert page.evaluate('brush.base') == 'empty', 'Automatic mask must not silently choose the movement'
        expect(page.locator('#brush-smart')).to_be_checked()
        # Deliberately letterbox the square source. Strokes must remain in image
        # coordinates, not accidentally include the surrounding CSS space.
        page.locator('#image-canvas').evaluate("canvas => {canvas.style.width='300px';canvas.style.height='150px';}")
        draw_stroke(page, touch=context if mobile else None)
        page.locator('#image-canvas').evaluate("canvas => {canvas.style.width='';canvas.style.height='';}")
        expect(page.locator('#brush-apply')).to_be_enabled()
        page.locator('#brush-undo').click()
        expect(page.locator('#brush-apply')).to_be_disabled()
        page.locator('#brush-redo').click()
        expect(page.locator('#brush-apply')).to_be_enabled()
        page.locator('#brush-erase').click()
        draw_stroke(page, (.45, .4), (.5, .5), touch=context if mobile else None)
        payload = page.evaluate('brush.payload()')
        assert payload['smart'] is True and payload['base'] == 'empty'
        assert [stroke['mode'] for stroke in payload['strokes']] == ['add', 'erase']
        first = payload['strokes'][0]['points'][0]
        assert abs(first[0] - .25) < .03 and abs(first[1] - .35) < .03, first
        last = payload['strokes'][0]['points'][-1]
        assert abs(last[0] - .55) < .03 and abs(last[1] - .55) < .03, last
        assert not any(path.endswith('/paint') for _, path, _ in requests), 'Painting must not send one request per stroke'
        # Backend errors leave painting intact and controls retryable.
        failure['paint'] = True
        page.locator('#brush-apply').click()
        expect(page.locator('#brush-apply')).to_be_enabled()
        expect(page.locator('#brush-panel')).to_be_visible()
        assert page.evaluate('brush.strokes.length') == 2
        failure['paint'] = False
        # A hung network request must time out and preserve the edit for retry.
        before_timeout = page.evaluate('brush.payload()')
        page.evaluate('''() => {
          window.__brushRealFetch = window.fetch;
          window.fetch = (url, options) => String(url).endsWith('/paint')
            ? new Promise(() => {}) : window.__brushRealFetch(url, options);
        }''')
        page.clock.install()
        page.locator('#brush-apply').click()
        expect(page.locator('#brush-apply')).to_be_disabled()
        expect(page.locator('#render-overlay')).to_be_visible()
        page.clock.fast_forward(61000)
        expect(page.locator('#render-overlay')).to_be_hidden()
        expect(page.locator('#brush-apply')).to_be_enabled()
        expect(page.locator('#brush-panel')).to_be_visible()
        assert page.evaluate('brush.payload()') == before_timeout
        page.evaluate('window.fetch = window.__brushRealFetch')
        page.clock.resume()
        page.locator('#brush-apply').click()
        expect(page.locator('#brush-panel')).to_be_hidden()
        expect(page.locator('#render-button')).to_be_enabled()
        expect(page.locator('#select-head')).to_be_visible()
        assert not any(path.endswith('/render') for _, path, _ in requests)
        page.locator('#paint-region').click()
        assert page.evaluate('brush.base') == 'current', 'Saved selection should be refinable'
        page.locator('#brush-clear').click()
        assert page.evaluate('brush.base') == 'empty'
        # Keyboard-only input can create an exact manual selection.
        page.locator('#brush-smart').uncheck()
        page.locator('#brush-add').click()
        page.locator('#image-canvas').focus()
        page.keyboard.press('ArrowRight')
        page.keyboard.down('Space')
        page.keyboard.press('ArrowRight')
        page.keyboard.press('ArrowDown')
        page.keyboard.up('Space')
        payload = page.evaluate('brush.payload()')
        assert payload['smart'] is False and payload['base'] == 'empty'
        assert len(payload['strokes']) == 1 and len(payload['strokes'][0]['points']) == 3
        page.locator('#brush-cancel').click()
        expect(page.locator('#brush-panel')).to_be_hidden()
        expect(page.locator('#render-button')).to_be_enabled()
        # Choosing the whole image remains an explicit, reversible option.
        page.locator('[data-mode="whole"]').click()
        expect(page.locator('#render-button')).to_be_enabled()
        expect(page.locator('#select-head')).to_be_hidden()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
    finally:
        context.close()


def main():
    handler = partial(Handler, directory=str(ROOT / 'headbop/static'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            selected = os.getenv('HEADBOP_BROWSER_EXECUTABLE')
            executable = None if selected == 'playwright' else (selected or shutil.which('chromium'))
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                for mobile in (False, True):
                    run_case(browser, f'http://127.0.0.1:{server.server_port}', mobile=mobile)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print('PASS: desktop/touch/keyboard paint, letterboxed normalized coordinates, smart/manual, add/erase, undo/redo, refinement, failed/stalled-apply recovery, no network request per stroke; no automatic render after analysis or paint; explicit whole/head choices; no JS errors.')


if __name__ == '__main__':
    main()
