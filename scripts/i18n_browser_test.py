"""Browser regressions for language bindings, accessibility and request deadlines.

Uses real Chromium and real DOM/UI events with deterministic API responses. Actual
FFmpeg, upload and download integration is covered by browser_smoke.py separately.
Run: python scripts/i18n_browser_test.py
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
LANGUAGES = ('pt-BR', 'en', 'es', 'fr', 'de', 'it', 'zh-CN', 'hi')
CATALOGS = json.JSONDecoder().raw_decode(
    (ROOT / 'headbop/static/i18n.js').read_text().split('const translations = ', 1)[1]
)[0]
IMAGE = io.BytesIO()
Image.new('RGBA', (64, 64), (100, 160, 70, 255)).save(IMAGE, format='PNG')
PROJECT = {
    'id': 'a' * 32, 'width': 64, 'height': 64, 'duration': 4,
    'image_url': '/fixture.png', 'foreground_url': '/fixture.png',
    'settings': {'mode': 'subject', 'motion': 'auto', 'engine': 'v2', 'bpm': 120,
                 'intensity': 1, 'fps': 24, 'resolution': 640, 'aspect': 'source',
                 'speed': 1, 'easing': 'natural', 'squash': 0.25},
    'analysis': {'bpm': 120, 'beats': [0, 1, 2], 'intensity': [1, 0.5, 0.8]},
    'keyframes': [{'time': 0}, {'time': 1}],
    'segmentation': {'available': True, 'warning':
                     'Recorte por contraste: confira as bordas. O preenchimento do fundo no vídeo pode deixar artefatos.'},
    'rig': {'available': True, 'kind': 'whole', 'warning':
            'Recorte preservado como uma camada. Selecione a cabeça manualmente para articular cabeça e corpo.'},
}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def translate_path(self, path):
        if path.startswith('/static/'):
            path = path[len('/static'):]
        return super().translate_path(path)

    def do_GET(self):
        if urlsplit(self.path).path == '/fixture.png':
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(IMAGE.getvalue())))
            self.end_headers()
            self.wfile.write(IMAGE.getvalue())
            return
        super().do_GET()


def api(route):
    path = urlsplit(route.request.url).path
    if path == '/api/auth/session':
        body = {'user': {'id': 'b' * 32, 'username': 'browser-test'}, 'csrf': 'test-csrf',
                'expires_at': time.time() + 3600}
    elif path == '/api/projects':
        body = {'projects': []}
    elif path.endswith('/render'):
        body = {'status': 'rendering', 'progress': 37}
    else:
        body = PROJECT
    route.fulfill(status=200, content_type='application/json', body=json.dumps(body))


def translate(page, language):
    page.locator('#language-select').select_option(language)
    expect(page.locator('html')).to_have_attribute('lang', language)


def overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def check_bindings(page, language):
    expected = CATALOGS[language]
    expect(page.locator('#source-title')).to_contain_text(expected['Seus arquivos'])
    expect(page.locator('#beat-summary')).to_have_text(
        expected['summary.beats'].replace('{duration}', '4.0').replace('{count}', '2')
    )
    expect(page.locator('#segmentation-message')).to_have_text(
        expected[PROJECT['segmentation']['warning']]
    )
    expect(page.locator('#selection-instruction')).to_have_text(
        expected['Arraste um retângulo ao redor da cabeça. O pivô ficará na base, perto do pescoço.']
    )
    expect(page.locator('#render-progress-track')).to_have_attribute(
        'aria-valuetext', expected['Processando…']
    )
    expect(page.locator('#render-label')).to_have_text(expected['Na fila de renderização…'])
    expect(page.locator('#image-name')).to_have_text('Gerar preview')  # User data, not a key.
    expect(page.locator('#notice')).to_have_text(
        expected['error.rate_limited'].replace('{seconds}', '7')
    )
    expect(page.locator('#account-user')).to_have_text(
        expected['auth.signedInAs'].replace('{name}', 'browser-test')
    )
    # Percent descriptions must rebind just like the indeterminate description.
    page.evaluate('updateProgress(37)')
    expect(page.locator('#render-progress-track')).to_have_attribute(
        'aria-valuetext', expected['progress.percent'].replace('{percent}', '37')
    )
    page.evaluate('updateProgress()')
    overflow(page)


def deadline_checks(page):
    # A request must expire even when fetch itself ignores its AbortSignal.
    result = page.evaluate('''async () => {
      const original = window.fetch;
      let aborted = false;
      window.fetch = (_url, options) => {
        options.signal.addEventListener('abort', () => { aborted = true; });
        return new Promise(() => {});
      };
      const started = performance.now();
      try { await request('/test', {timeout: 70}); }
      catch (error) { return {key: error.messageKey, aborted, elapsed: performance.now()-started}; }
      finally { window.fetch = original; }
    }''')
    assert result['key'] == 'error.request_timeout' and result['aborted'], result
    assert result['elapsed'] < 2000, result
    # The deadline must also include a response body that never finishes.
    result = page.evaluate('''async () => {
      const original = window.fetch;
      let aborted = false;
      window.fetch = async (_url, options) => {
        options.signal.addEventListener('abort', () => { aborted = true; });
        return new Response(new ReadableStream({start() {}}), {status:200});
      };
      try { await request('/test', {timeout:70}); }
      catch (error) { return {key:error.messageKey, aborted}; }
      finally { window.fetch = original; }
    }''')
    assert result == {'key': 'error.request_timeout', 'aborted': True}, result
    # A past overall deadline must prevent another HTTP request entirely.
    result = page.evaluate('''async () => {
      const original = window.fetch;
      let called = false;
      window.fetch = () => { called = true; return new Promise(() => {}); };
      try { await request('/test', {deadline:Date.now()-1}); }
      catch (error) { return {key:error.messageKey, called}; }
      finally { window.fetch = original; }
    }''')
    assert result == {'key': 'error.request_timeout', 'called': False}, result
    # Exercise the render button state after a stalled initial POST, not just request().
    page.evaluate('''() => {
      cancelSelection(); setBusy(false); state.dirty = false;
      window.__realFetch = window.fetch;
      window.fetch = (url, options) => String(url).endsWith('/render')
        ? new Promise(() => {}) : window.__realFetch(url, options);
    }''')
    page.clock.install()
    page.locator('#render-button').click()
    expect(page.locator('#render-overlay')).to_be_visible()
    page.clock.fast_forward(31000)
    expect(page.locator('#render-overlay')).to_be_hidden()
    expect(page.locator('#render-button')).to_be_enabled()
    expect(page.locator('#notice')).to_have_text(CATALOGS['en']['error.request_timeout'])
    page.evaluate('window.fetch = window.__realFetch')
    # A healthy queued job can wait behind a full render. Budget comes from the
    # initial response, and repeated successful polls cannot prolong it.
    result = page.evaluate('''() => [undefined, null, -1, 0, Infinity, "630", 60000, 630, 3630]
      .map(timeout_seconds => renderBudgetSeconds({timeout_seconds}))''')
    assert result == [630, 630, 630, 630, 630, 630, 3630, 630, 3630], result
    page.evaluate('''() => {
      state.dirty = false;
      window.fetch = async (url, options) => String(url).endsWith('/render')
        ? Response.json({status:'queued',progress:0,timeout_seconds:630})
        : window.__realFetch(url, options);
    }''')
    page.locator('#render-button').click()
    expect(page.locator('#render-label')).to_have_text(CATALOGS['en']['Na fila de renderização…'])
    page.clock.fast_forward(301000)
    expect(page.locator('#render-overlay')).to_be_visible()
    expect(page.locator('#render-button')).to_be_disabled()
    page.clock.fast_forward(330000)
    expect(page.locator('#render-overlay')).to_be_hidden()
    expect(page.locator('#render-button')).to_be_enabled()
    expect(page.locator('#notice')).to_have_text(CATALOGS['en']['error.request_timeout'])
    page.evaluate('window.fetch = window.__realFetch')
    page.clock.resume()


def security_and_error_bindings(page):
    result = page.evaluate('''async () => {
      const original = window.fetch;
      let csrf;
      window.fetch = async (_url, options) => {
        csrf = options.headers.get('X-CSRF-Token');
        return Response.json({ok:true});
      };
      try { await request('/test', {method:'POST'}); return csrf; }
      finally { window.fetch = original; }
    }''')
    assert result == 'test-csrf', result
    # Known server instructions retain their meaning; untrusted text is never shown.
    result = page.evaluate('''() => ({
      known: headbopI18n.errorMessage({code:'invalid_request',detail:'Use uma imagem PNG, JPEG ou WebP.'}),
      unknown: headbopI18n.errorMessage({detail:'<img src=x onerror=alert(1)>'}),
      null: headbopI18n.errorMessage(null)
    })''')
    assert result == {
        'known': {'key': 'Use uma imagem PNG, JPEG ou WebP.', 'params': {}},
        'unknown': {'key': 'error.unknown', 'params': {}},
        'null': {'key': 'error.unknown', 'params': {}},
    }, result
    page.evaluate('''async () => {
      const original = window.fetch;
      window.fetch = async () => Response.json({code:'auth_required'}, {status:401});
      try { await request('/api/projects'); } catch (_) {} finally { window.fetch = original; }
    }''')
    expect(page.locator('#studio')).to_be_hidden()
    expect(page.locator('#auth-panel')).to_be_visible()
    for language in LANGUAGES:
        translate(page, language)
        expect(page.locator('#auth-notice')).to_have_text(CATALOGS[language]['auth.sessionExpired'])
        expect(page.locator('#login-button')).to_have_text(CATALOGS[language]['auth.signIn'])
        overflow(page)


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(
        Handler, directory=str(ROOT / 'headbop/static')
    ))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            selected = os.getenv('HEADBOP_BROWSER_EXECUTABLE')
            executable = None if selected == 'playwright' else selected or shutil.which('chromium')
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                for width in (1440, 390, 320):
                    context = browser.new_context(viewport={'width': width, 'height': 900},
                                                  is_mobile=width < 500, has_touch=width < 500,
                                                  reduced_motion='reduce', locale='pt-BR')
                    context.route('**/api/**', api)
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/')
                    expect(page.locator('#studio')).to_be_visible()
                    for language in LANGUAGES:
                        translate(page, language)
                        page.reload()
                        expect(page.locator('#language-select')).to_have_value(language)
                        expect(page.locator('#studio')).to_be_visible()
                        overflow(page)
                    page.evaluate('(project) => loadProject(project)', PROJECT)
                    page.evaluate('''() => {
                      chooseFile('image', new File(['x'], 'Gerar preview'));
                      startSelection('head'); setBusy(true, 'Na fila de renderização…');
                      notice({key:'error.rate_limited',params:{seconds:7}},true);
                    }''')
                    for language in LANGUAGES:
                        translate(page, language)
                        check_bindings(page, language)
                    translate(page, 'en')
                    page.evaluate('updateProgress(37)')
                    expect(page.locator('#render-progress-track')).to_have_attribute('aria-valuenow', '37')
                    expect(page.locator('#render-progress-track')).to_have_attribute(
                        'aria-valuetext', CATALOGS['en']['progress.percent'].replace('{percent}', '37')
                    )
                    # Repeated changes settle without observer feedback or idle rewrites.
                    count = page.evaluate('''async () => {
                      for (let index=0;index<100;index++) headbopI18n.setLanguage(index%2 ? 'en' : 'de');
                      await new Promise(resolve => setTimeout(resolve, 50));
                      let count = 0;
                      const observer = new MutationObserver(records => { count += records.length; });
                      observer.observe(document.body, {subtree:true,childList:true,attributes:true,characterData:true});
                      await new Promise(resolve => setTimeout(resolve, 300));
                      observer.disconnect(); return count;
                    }''')
                    assert count == 0, count
                    if width == 1440:
                        deadline_checks(page)
                    security_and_error_bindings(page)
                    assert not errors, errors
                    context.close()
                print('PASS: 8 locales × desktop/390px/320px; persistence; dynamic instructions, warnings, '
                      'summary, progress ARIA; filenames preserved; 100 switches settle; fetch/body deadlines '
                      'and render POST failure release busy state; queued jobs use a fixed server budget; '
                      'CSRF, expired-session UI and error bindings; no JS errors.')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
