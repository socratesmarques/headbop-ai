"""Public browser workspaces keep private ownership and non-resettable budgets."""

from contextlib import ExitStack
import json
import time

from fastapi.testclient import TestClient
import pytest

from headbop.auth import AuthStore
from headbop.main import create_app
from headbop.security import network_scope

ORIGIN = 'http://testserver'


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv('HEADBOP_ENV', 'development')
    monkeypatch.delenv('HEADBOP_PUBLIC_ORIGIN', raising=False)
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('203.0.113.10', 50000)) as first, ExitStack() as others:
        first.headers['Origin'] = ORIGIN

        def new_client(peer='203.0.113.10'):
            client = TestClient(app, client=(peer, 50001))
            client.headers['Origin'] = ORIGIN
            others.callback(client.close)
            return client
        yield first, new_client


def workspace(client):
    response = client.post('/api/auth/guest')
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    return response


def project(client, *, size=0):
    user = client.get('/api/auth/session').json()['user']
    store = client.app.state.store
    project_id, path = store.create(user['id'])
    store.write({'id': project_id, 'width': 64, 'height': 64, 'duration': 1,
                 'analysis': {'beats': [], 'intensity': []},
                 'segmentation': {'available': False},
                 'settings': {'engine': 'legacy', 'mode': 'whole', 'motion': 'hard',
                              'bpm': 120, 'intensity': 1},
                 'keyframes': [], 'render': {'status': 'idle', 'video_url': None, 'error': None}})
    with (path / 'image.png').open('wb') as output:
        output.truncate(size)
    store.building.discard(project_id)
    return project_id


def test_first_visitor_starts_without_password_and_reuses_workspace(clients, monkeypatch):
    first, _ = clients
    monkeypatch.setattr('headbop.auth._derive', lambda *_: pytest.fail('Guest bootstrap must not hash a password'))
    response = workspace(first)
    body = response.json()
    assert set(body) == {'user', 'csrf', 'expires_at'}
    assert body['user']['guest'] is True
    assert 'token' not in body
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert 'samesite=strict' in response.headers['set-cookie'].lower()
    assert first.get('/api/auth/session').json() == body
    for _ in range(12):
        assert first.post('/api/auth/guest').json() == body
    with first.app.state.auth._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM guest_owners').fetchone()[0] == 1
    assert first.app.state.auth.list_users() == []


def test_guest_bootstrap_preserves_existing_named_account(clients):
    first, _ = clients
    first.app.state.auth.create_user('alice', 'Alice-password-1234')
    login = first.post('/api/auth/login', json={'username': 'alice', 'password': 'Alice-password-1234'})
    assert login.status_code == 200
    assert workspace(first).json() == login.json()


def test_guest_identity_cannot_be_used_as_password_account(clients):
    first, _ = clients
    user = workspace(first).json()['user']
    # Even an administrator assigning a password must not turn a guest into a
    # discoverable login identity. Legacy named accounts remain independent.
    first.app.state.auth.reset_password(user['username'], 'Changed-password-1234')
    response = first.post('/api/auth/login', json={'username': user['username'], 'password': 'Changed-password-1234'})
    assert response.status_code == 401


@pytest.mark.parametrize('method,suffix', [
    ('GET', ''), ('DELETE', ''), ('GET', '/image'), ('GET', '/foreground'),
    ('GET', '/video'), ('GET', '/export'), ('GET', '/render'), ('POST', '/render'),
    ('PUT', '/settings'), ('POST', '/segment'), ('POST', '/rig'), ('POST', '/paint'),
])
def test_browser_identity_is_isolated_even_on_same_network(clients, method, suffix):
    first, new_client = clients
    first_id = workspace(first).json()['user']['id']
    project_id = project(first)
    second = new_client()
    assert workspace(second).json()['user']['id'] != first_id
    assert second.get('/api/projects').json() == {'projects': []}
    response = second.request(method, '/api/projects/' + project_id + suffix)
    assert response.status_code == 404


def test_guest_requires_origin_and_writes_require_csrf(clients):
    first, _ = clients
    assert first.post('/api/auth/guest', headers={'Origin': 'https://attacker.example'}).status_code == 403
    workspace(first)
    before = first.get('/api/auth/session').json()
    assert first.post('/api/auth/logout', headers={'X-CSRF-Token': ''}).status_code == 403
    assert first.get('/api/auth/session').json() == before
    assert first.post('/api/projects', content=b'invalid', headers={'X-CSRF-Token': ''}).status_code == 403


def test_clearing_cookies_or_forging_xff_does_not_reset_creation_rate(clients):
    first, new_client = clients
    ids = set()
    for index in range(10):
        client = first if index == 0 else new_client()
        client.headers['X-Forwarded-For'] = f'198.51.100.{index}'
        ids.add(workspace(client).json()['user']['id'])
    assert len(ids) == 10
    rejected = new_client().post('/api/auth/guest')
    assert rejected.status_code == 429 and int(rejected.headers['Retry-After']) > 0
    assert workspace(new_client('203.0.113.20')).status_code == 200
    assert workspace(first).status_code == 200  # Returning visitors are not new workspaces.


def test_cookie_reset_does_not_reset_project_quota(clients):
    first, new_client = clients
    workspace(first)
    first.app.state.store.user_max_projects = 1
    project(first)
    second = new_client()
    workspace(second)
    response = second.post('/api/projects', content=b'invalid')
    assert response.status_code == 429 and response.json()['code'] == 'quota_projects'
    third = new_client('203.0.113.20')
    workspace(third)
    assert third.post('/api/projects', content=b'invalid').status_code == 422
    assert not first.app.state.store.reservations
    assert not first.app.state.admission.users


def test_cookie_reset_does_not_reset_disk_quota(clients):
    first, new_client = clients
    workspace(first)
    project(first, size=130 * 1024 * 1024)
    second = new_client()
    workspace(second)
    response = second.post('/api/import', content=b'invalid')
    assert response.status_code == 429 and response.json()['code'] == 'quota_storage'
    assert not first.app.state.store.reservations
    assert not first.app.state.admission.users


@pytest.mark.parametrize('budget,code', [('projects', 'quota_projects'), ('storage', 'quota_storage')])
def test_guest_created_during_admission_is_included_in_quota(clients, monkeypatch, budget, code):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    first, new_client = clients
    workspace(first)
    store = first.app.state.store
    if budget == 'projects':
        store.user_max_projects = 1
    else:
        store.user_storage = 128 * 1024 * 1024
    admission = first.app.state.admission
    original_enter = admission.enter
    waiting, release = threading.Event(), threading.Event()

    def delayed_enter(key):
        waiting.set()
        assert release.wait(5)
        return original_enter(key)

    monkeypatch.setattr(admission, 'enter', delayed_enter)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(first.post, '/api/projects', content=b'invalid')
        try:
            assert waiting.wait(5)
            # Another browser can finish creating its workspace and project
            # before the first request obtains its analysis admission slot.
            second = new_client()
            workspace(second)
            project(second)
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 429 and response.json()['code'] == code
    assert not store.reservations
    assert not admission.users


def test_guest_cleanup_retains_project_owner_and_rate_scope(tmp_path):
    clock = [time.time()]
    auth = AuthStore(tmp_path / 'security.sqlite3', clock=lambda: clock[0])
    retained = auth.create_guest('203.0.113.10')['user']['id']
    abandoned = auth.create_guest('203.0.113.10')['user']['id']
    clock[0] += 2 * 86400
    auth.cleanup({retained})
    with auth._connect() as db:
        assert db.execute('SELECT id FROM users WHERE id=?', (retained,)).fetchone()
        assert db.execute('SELECT id FROM users WHERE id=?', (abandoned,)).fetchone() is None
    new = auth.create_guest('203.0.113.10')['user']['id']
    assert auth.guest_owners(new) == {retained, new}
    auth.cleanup()
    assert auth.guest_owners(new) == {new}


def test_guest_owner_scope_persists_after_database_reopen(tmp_path):
    path = tmp_path / 'security.sqlite3'
    auth = AuthStore(path)
    first = auth.create_guest('203.0.113.10')
    auth = AuthStore(path)
    assert auth.get_session(first['token'])['user'] == first['user']
    second = auth.create_guest('203.0.113.10')
    assert auth.guest_owners(second['user']['id']) == {first['user']['id'], second['user']['id']}
    assert first['user']['id'] not in auth.guest_owners(auth.create_guest('203.0.113.20')['user']['id'])
    raw = json.dumps(first['user'])
    assert '203.0.113.10' not in raw and 'scope' not in raw


def test_ipv6_privacy_addresses_share_network_budget():
    assert network_scope('2001:db8:abcd:1::a') == network_scope('2001:db8:abcd:1::b')
    assert network_scope('2001:db8:abcd:2::a') != network_scope('2001:db8:abcd:1::b')


def test_guest_analysis_admission_cannot_be_evaded_by_another_cookie(clients, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    first, new_client = clients
    workspace(first)
    second, third = new_client(), new_client('203.0.113.20')
    workspace(second)
    workspace(third)
    entered, release = threading.Event(), threading.Event()

    def blocked_image(*_args):
        entered.set()
        assert release.wait(5)
        raise ValueError('Invalid test media')

    monkeypatch.setattr('headbop.main.normalize_image', blocked_image)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(first.post, '/api/projects', files={
            'image': ('image.png', b'invalid', 'image/png'),
            'audio': ('audio.wav', b'invalid', 'audio/wav'),
        })
        try:
            assert entered.wait(5)
            denied = second.post('/api/projects', content=b'invalid')
            assert denied.status_code == 429 and denied.json()['code'] == 'capacity_busy'
            assert third.post('/api/projects', content=b'invalid').status_code == 422
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 422
    assert not first.app.state.admission.users
    assert not first.app.state.store.reservations


def test_guest_render_queue_is_shared_by_network_not_browser_cookie(clients, monkeypatch):
    import threading

    first, new_client = clients
    workspace(first)
    second, third = new_client(), new_client('203.0.113.20')
    workspace(second)
    workspace(third)
    first_project, second_project, third_project = project(first), project(second), project(third)
    entered, release = threading.Event(), threading.Event()

    def blocked_render(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        raise RuntimeError('Test rendering failure')

    monkeypatch.setattr('headbop.main.run_render_job', blocked_render)
    try:
        assert first.post(f'/api/projects/{first_project}/render').status_code == 202
        assert entered.wait(5)
        denied = second.post(f'/api/projects/{second_project}/render')
        assert denied.status_code == 429 and denied.json()['code'] == 'capacity_busy'
        assert third.post(f'/api/projects/{third_project}/render').status_code == 202
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while first.app.state.store.active and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not first.app.state.store.active
    assert not first.app.state.store.reservations
