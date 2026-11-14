"""Exercise the production Compose configuration through locally trusted HTTPS.

Run after building the application image:
  python scripts/staging_smoke.py --image headbop-ai:public-studio
Requires Docker Compose 2.24.4+ (ports !override), local Docker, ffprobe, and dev
requirements. The isolated Compose project is stopped afterwards; named volumes
are preserved. Only loopback ports are published. No DNS/ACME claim is made.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
from xml.etree import ElementTree as ET

import httpx

from browser_smoke import media_files

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'test-results'
SELECTORS = ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='headbop-ai:public-studio')
    parser.add_argument('--project', default='headbop-public-studio-audit')
    parser.add_argument('--http-port', type=int, default=18084)
    parser.add_argument('--https-port', type=int, default=18444)
    args = parser.parse_args()
    if not args.project.startswith('headbop-'):
        parser.error('Use a dedicated headbop- prefixed project name')
    RESULTS.mkdir(exist_ok=True)
    result = {}
    environment = {key: value for key, value in os.environ.items() if key not in SELECTORS}
    environment['HEADBOP_DOMAIN'] = 'localhost'
    docker = ['docker', '--host=unix:///var/run/docker.sock']

    def run(command, *, input=None, timeout=60):
        completed = subprocess.run(command, input=input, text=True, capture_output=True,
                                   env=environment, cwd=ROOT, timeout=timeout)
        if completed.returncode:
            # No command here includes passwords or session tokens.
            raise RuntimeError(f'Command failed ({completed.returncode}): {command}\n{completed.stderr}')
        return completed.stdout

    def step(label):
        print(label, flush=True)

    run([*docker, 'info', '--format', '{{.ServerVersion}}'])
    with tempfile.TemporaryDirectory(prefix='headbop-staging-') as temporary:
        directory = Path(temporary)
        origin = f'https://localhost:{args.https_port}'
        config = directory / 'Caddyfile'
        config.write_text((ROOT / 'Caddyfile.staging').read_text().replace(
            '{$HEADBOP_DOMAIN} {', '{$HEADBOP_DOMAIN} {\n  tls internal', 1))
        config.chmod(0o644)
        # Keep proxy settings unchanged; only direct this private service name
        # around a session's outbound proxy. Do not alter registry credentials.
        docker_config = Path(os.environ.get('DOCKER_CONFIG', str(Path.home() / '.docker'))) / 'config.json'
        configured_no_proxy = ''
        if docker_config.is_file():
            configured_no_proxy = json.loads(docker_config.read_text()).get('proxies', {}).get('default', {}).get('noProxy', '')
        no_proxy = ','.join(filter(None, [configured_no_proxy, os.getenv('NO_PROXY', ''),
                                         os.getenv('no_proxy', ''), 'localhost,127.0.0.1,::1,app']))
        override = directory / 'override.yml'
        override.write_text(f'''services:
  app:
    image: {json.dumps(args.image)}
    environment:
      HEADBOP_PUBLIC_ORIGIN: {json.dumps(origin)}
  caddy:
    ports: !override
      - "127.0.0.1:{args.http_port}:80"
      - "127.0.0.1:{args.https_port}:443"
    environment:
      NO_PROXY: {json.dumps(no_proxy)}
      no_proxy: {json.dumps(no_proxy)}
    volumes:
      - {json.dumps(str(config) + ':/etc/caddy/Caddyfile:ro')}
''')
        compose = [*docker, 'compose', '-p', args.project, '-f', str(ROOT / 'compose.staging.yml'),
                   '-f', str(override)]
        if run([*compose, 'ps', '-q']).strip():
            raise RuntimeError('Audit project already has running containers; choose a different project')
        started = False
        try:
            run([*compose, 'config', '--quiet'])
            step('Starting isolated production Compose with local TLS')
            started = True
            run([*compose, 'up', '-d', '--no-build'], timeout=60)
            app_id = run([*compose, 'ps', '-q', 'app']).strip()
            caddy_id = run([*compose, 'ps', '-q', 'caddy']).strip()
            run([*docker, 'exec', caddy_id, 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile'])
            ca = directory / 'root.crt'
            for _ in range(40):
                copied = subprocess.run([*docker, 'cp', f'{caddy_id}:/data/caddy/pki/authorities/local/root.crt', str(ca)],
                                        capture_output=True, env=environment)
                if copied.returncode == 0:
                    break
                time.sleep(0.25)
            assert ca.is_file(), 'Caddy did not generate its local CA'
            context = ssl.create_default_context(cafile=str(ca))
            clients = []

            def client():
                instance = httpx.Client(base_url=origin, verify=context, timeout=90,
                                        headers={'Origin': origin})
                clients.append(instance)
                return instance

            anonymous, alice, bob = client(), client(), client()
            for _ in range(40):
                try:
                    response = anonymous.get('/api/health')
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.25)
            assert response.status_code == 200 and response.json()['ffmpeg']
            result['tls_verified_against_local_ca'] = True
            assert anonymous.get('/').status_code == 200
            assert anonymous.get('/static/auth.js').status_code == 200
            for path in ('/api/projects', '/api/auth/session', '/openapi.json', '/api/projects/' + '0' * 32 + '/export'):
                assert anonymous.get(path).status_code == 401, path
            result['anonymous_api_denied'] = True
            assert anonymous.post('/api/auth/guest', headers={'Origin': 'https://attacker.example'}).status_code == 403
            identities = []
            for account in (alice, bob):
                opened = account.post('/api/auth/guest')
                assert opened.status_code == 200, opened.text
                cookie = opened.headers['set-cookie'].lower()
                assert all(flag in cookie for flag in ('secure', 'httponly', 'samesite=strict'))
                payload = opened.json()
                assert 'token' not in payload and payload['user']['guest'] is True
                identities.append(payload['user']['id'])
                account.headers['X-CSRF-Token'] = payload['csrf']
                assert account.post('/api/auth/guest').json() == payload
            assert identities[0] != identities[1]
            result['secure_private_guest_sessions_without_provisioning'] = True
            assert alice.post('/api/auth/logout', headers={'X-CSRF-Token': ''}).status_code == 403
            assert alice.post('/api/auth/logout', headers={'Origin': 'https://attacker.example'}).status_code == 403
            result['csrf_and_origin_denied'] = True
            inspected = json.loads(run([*docker, 'inspect', app_id]))[0]
            host = inspected['HostConfig']
            assert host['ReadonlyRootfs'] and host['NanoCpus'] == 2_000_000_000
            assert host['Memory'] == 4 * 1024**3 and host['PidsLimit'] == 128
            assert 'ALL' in host['CapDrop'] and 'no-new-privileges:true' in host['SecurityOpt']
            assert not inspected['NetworkSettings']['Ports'].get('8000/tcp')
            assert run([*docker, 'exec', app_id, 'id', '-u']).strip() == '10001'
            root_write = run([*docker, 'exec', app_id, 'python', '-c',
                              'import os; print(os.access("/app", os.W_OK)); print(os.access("/data", os.W_OK))'])
            assert root_write.splitlines() == ['False', 'True']
            result['container_limits'] = {'uid': 10001, 'cpus': 2, 'memory_gib': 4, 'pids': 128,
                                          'read_only_root': True, 'private_backend': True, 'cap_drop_all': True}
            step('Checking guest upload, intelligent painting, H.264/AAC rendering and XML roundtrip')
            image, audio = media_files(directory)
            uploaded = alice.post('/api/projects', files={
                'image': (image.name, image.read_bytes(), 'image/png'),
                'audio': (audio.name, audio.read_bytes(), 'audio/wav'),
            })
            assert uploaded.status_code == 201, uploaded.text
            project = uploaded.json()
            prefix = '/api/projects/' + project['id']
            assert alice.get(prefix + '/render').json()['status'] == 'idle'
            assert alice.get(prefix + '/video').status_code == 409
            paint_request = {'strokes': [{'points': [[.5, .25], [.5, .45]], 'radius': .04, 'mode': 'add'}],
                             'smart': True, 'base': 'empty'}
            assert alice.post(prefix + '/paint', json=paint_request, headers={'X-CSRF-Token': ''}).status_code == 403
            assert bob.post(prefix + '/paint', json=paint_request).status_code == 404
            painted = alice.post(prefix + '/paint', json=paint_request)
            assert painted.status_code == 200, painted.text
            project = painted.json()
            assert project['settings']['mode'] == 'subject'
            assert project['segmentation']['method'] == 'smart-brush'
            assert alice.get(prefix + '/render').json()['status'] == 'idle'
            assert alice.get(prefix + '/video').status_code == 409
            foreground = alice.get(project['foreground_url'])
            assert foreground.status_code == 200
            from io import BytesIO
            from PIL import Image
            alpha = Image.open(BytesIO(foreground.content)).getchannel('A')
            assert alpha.getextrema() == (0, 255)
            assert alpha.getpixel((160, 80)) == 255 and alpha.getpixel((20, 20)) == 0
            result['smart_brush_selects_head_and_waits_for_explicit_render'] = True
            update = alice.put(prefix + '/settings', json={
                **project['settings'], 'mode': 'subject', 'fps': 30, 'resolution': 720, 'aspect': 'vertical',
            })
            assert update.status_code == 200, update.text
            project = update.json()
            assert bob.get('/api/projects').json() == {'projects': []}
            for suffix in ('', '/image', '/foreground', '/video', '/export', '/render'):
                assert bob.get(prefix + suffix).status_code == 404, suffix
            assert bob.delete(prefix).status_code == 404
            assert bob.post(prefix + '/render').status_code == 404
            assert bob.put(prefix + '/settings', json=project['settings']).status_code == 404
            result['cross_guest_denied'] = True
            start = alice.post(prefix + '/render')
            assert start.status_code == 202, start.text
            samples = []
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                status = alice.get(prefix + '/render').json()
                samples.append({'status': status['status'], 'progress': status['progress']})
                assert status['progress'] == 100 if status['status'] == 'ready' else status['progress'] < 100
                if status['status'] in {'ready', 'error'}:
                    break
                time.sleep(0.2)
            assert status['status'] == 'ready', status
            video = alice.get(prefix + '/video')
            download = alice.get(prefix + '/video?download=true')
            assert video.status_code == 200 and video.content == download.content
            video_path = directory / 'render.mp4'
            video_path.write_bytes(video.content)
            probe = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(video_path)]))
            streams = probe['streams']
            video_stream = next(stream for stream in streams if stream['codec_type'] == 'video')
            assert video_stream['codec_name'] == 'h264'
            assert (video_stream['width'], video_stream['height'], video_stream['avg_frame_rate']) == (720, 1280, '30/1')
            assert any(stream['codec_type'] == 'audio' and stream['codec_name'] == 'aac' for stream in streams)
            result['render_progress'] = samples
            result['mp4'] = {'width': 720, 'height': 1280, 'fps': 30, 'h264_aac': True,
                             'bytes': len(video.content), 'preview_equals_download': True}
            exported = alice.get(prefix + '/export')
            assert exported.status_code == 200 and ET.fromstring(exported.content).attrib['version'] == '2'
            imported = alice.post('/api/import', files={'file': ('project.xml', exported.content, 'application/xml')})
            assert imported.status_code == 201, imported.text
            assert imported.json()['keyframes'] == project['keyframes']
            result['xml_roundtrip'] = True
            assert alice.get(imported.json()['foreground_url']).content == foreground.content
            quota_projects = [imported.json()['id']]
            for _ in range(8):
                extra = alice.post('/api/import', files={'file': ('project.xml', exported.content, 'application/xml')})
                assert extra.status_code == 201, extra.text
                quota_projects.append(extra.json()['id'])
            quota = bob.post('/api/projects', content=b'invalid')
            assert quota.status_code == 429 and quota.json()['code'] == 'quota_projects', quota.text
            result['new_guest_cannot_reset_shared_project_quota'] = True
            step('Restarting application; checking persistent guest sessions, media, quotas and rate limits')
            run([*compose, 'restart', 'app'])
            for _ in range(80):
                try:
                    restored = alice.get(prefix)
                    if restored.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.25)
            assert restored.status_code == 200
            assert alice.get(prefix + '/video').content == video.content
            assert alice.get(prefix + '/render').json()['progress'] == 100
            assert bob.get(prefix).status_code == 404
            result['restart_preserves_sessions_ownership_mp4'] = True
            quota_after_restart = bob.post('/api/projects', content=b'invalid')
            assert quota_after_restart.status_code == 429 and quota_after_restart.json()['code'] == 'quota_projects'
            for project_id in quota_projects:
                assert alice.delete('/api/projects/' + project_id).status_code == 204
            assert alice.delete(prefix).status_code == 204
            assert alice.get(prefix).status_code == 404
            # Different spoofed XFF values must not evade Caddy's observed-IP bucket.
            guest_statuses = []
            for index in range(12):
                anonymous.cookies.clear()
                opened = anonymous.post('/api/auth/guest', headers={'X-Forwarded-For': f'203.0.113.{index + 1}'})
                guest_statuses.append(opened.status_code)
                if opened.status_code == 429:
                    assert int(opened.headers['Retry-After']) > 0
                    break
                assert opened.status_code == 200
            assert guest_statuses.count(200) == 8 and guest_statuses[-1] == 429, guest_statuses
            result['xff_spoof_and_cookie_reset_do_not_bypass_guest_limit'] = True
            assert alice.post('/api/auth/guest').status_code == 200
            run([*compose, 'restart', 'app'])
            for _ in range(80):
                try:
                    if anonymous.get('/api/health').status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.25)
            anonymous.cookies.clear()
            limited = anonymous.post('/api/auth/guest')
            assert limited.status_code == 429
            result['rate_limit_survives_restart'] = True
            response = httpx.get(f'http://localhost:{args.http_port}/', follow_redirects=False, timeout=10)
            assert response.status_code == 308 and response.headers['location'].startswith('https://localhost/')
            result['http_redirect'] = 308
            result['image_id'] = inspected['Image']
            result['passed'] = True
            step('PASS: production Compose, trusted local TLS, guest/CSRF/ownership, smart brush/video/XML, persistence and abuse limits')
        finally:
            for instance in locals().get('clients', []):
                instance.close()
            (RESULTS / 'staging-results.json').write_text(json.dumps(result, indent=2) + '\n')
            if started:
                (RESULTS / 'staging-containers.log').write_text(run([*compose, 'logs', '--no-color']))
                run([*compose, 'down', '--remove-orphans'])  # Preserve all named volumes.


if __name__ == '__main__':
    main()
