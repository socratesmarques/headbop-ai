"""Real HTTP paint transactions, private guest ownership and portable mask fidelity."""

import io
from pathlib import Path
import time
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image, ImageDraw
import pytest
import soundfile as sf

from headbop.main import create_app, frames_for
from headbop.settings import Settings


def stroke(x=.5, y=.5, *, mode='add', radius=.04):
    return {'points': [[x, y]], 'radius': radius, 'mode': mode}


@pytest.fixture
def painted_client(tmp_path, monkeypatch):
    monkeypatch.setenv('HEADBOP_ENV', 'development')
    monkeypatch.delenv('HEADBOP_PUBLIC_ORIGIN', raising=False)
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('203.0.113.20', 50000)) as client:
        client.headers['Origin'] = 'http://testserver'
        session = client.post('/api/auth/guest').json()
        client.headers['X-CSRF-Token'] = session['csrf']
        store = app.state.store
        project_id, path = store.create(session['user']['id'])
        source = Image.new('RGBA', (200, 160), (230, 235, 240, 255))
        ImageDraw.Draw(source).rectangle((60, 40, 140, 120), fill=(200, 40, 30, 255))
        source.save(path / 'image.png')
        samples = np.zeros(22050 * 2, dtype=np.float32)
        for index, start in enumerate((.25, .75, 1.25, 1.75)):
            offset = round(start * 22050)
            pulse = np.random.default_rng(index).normal(size=800) * np.exp(-np.arange(800) / 180)
            samples[offset:offset + 800] = pulse * .4
        sf.write(path / 'audio.wav', samples, 22050, subtype='PCM_16')
        project = {'id': project_id, 'width': 200, 'height': 160, 'duration': 2,
                   'settings': Settings(bpm=120).model_dump(),
                   'analysis': {'bpm': 120, 'beats': [.25, .75, 1.25, 1.75],
                                'intensity': [1, 1, 1, 1]},
                   'segmentation': {'available': False, 'method': 'none', 'rect': None,
                                    'kind': 'unknown', 'head_rect': None, 'confidence': 0},
                   'image_url': f'/api/projects/{project_id}/image', 'foreground_url': None,
                   'render': {'status': 'idle', 'video_url': None, 'error': None}}
        project['keyframes'] = frames_for(project)
        store.write(project)
        store.building.discard(project_id)
        yield client, project_id, path


def apply(client, project_id, strokes=None, **kwargs):
    return client.post(f'/api/projects/{project_id}/paint', json={
        'strokes': strokes or [stroke()], 'smart': True, 'base': 'empty', **kwargs})


def snapshot(path):
    return {file.name: file.read_bytes() for file in path.iterdir() if file.is_file()}


def test_smart_paint_sets_subject_without_guessing_head_and_stays_idle(painted_client):
    client, project_id, path = painted_client
    before = client.app.state.store.read(project_id)
    before['keyframes'][0]['rotation'] = 17
    client.app.state.store.write(before)
    response = apply(client, project_id)
    assert response.status_code == 200, response.text
    project = response.json()
    assert project['settings']['mode'] == 'subject'
    assert project['settings']['engine'] == 'v2'
    assert project['segmentation']['method'] == 'smart-brush'
    assert project['rig']['kind'] == 'single' and project['rig']['head_rect'] is None
    assert project['keyframes'] != before['keyframes']
    assert client.get(f'/api/projects/{project_id}/render').json()['status'] == 'idle'
    assert not client.app.state.store.active
    assert not (path / 'video.mp4').exists()
    mask = np.asarray(Image.open(io.BytesIO(client.get(project['foreground_url']).content)))
    assert np.count_nonzero(mask[:, :, 3]) == 81 * 81
    assert np.all(mask[40:121, 60:141, 3] == 255)
    assert not client.app.state.store.reservations
    assert not client.app.state.admission.users


def test_explicit_head_articulation_changes_rig_without_replacing_paint(painted_client):
    client, project_id, path = painted_client
    assert apply(client, project_id).status_code == 200
    original_mask = (path / 'foreground.png').read_bytes()
    response = client.post(f'/api/projects/{project_id}/rig', json={'head_rect': [.3, .25, .4, .2]})
    assert response.status_code == 200, response.text
    assert response.json()['rig']['kind'] == 'head-body'
    assert response.json()['rig']['manual'] is True
    assert (path / 'foreground.png').read_bytes() == original_mask
    # A new paint selection intentionally clears the old articulation geometry.
    revised = apply(client, project_id, [stroke(.6, .5, mode='erase')], base='current')
    assert revised.status_code == 200
    assert revised.json()['rig']['kind'] == 'single'
    assert revised.json()['rig']['head_rect'] is None


def test_manual_refinement_erases_only_requested_area(painted_client):
    client, project_id, path = painted_client
    assert apply(client, project_id).status_code == 200
    response = apply(client, project_id, [stroke(.6, .5, mode='erase')], smart=False, base='current')
    assert response.status_code == 200, response.text
    mask = np.asarray(Image.open(path / 'foreground.png'))[:, :, 3]
    assert mask[80, 120] == 0 and mask[80, 100] == 255 and mask[80, 70] == 255


@pytest.mark.parametrize('failure', ['empty', 'rig', 'replace', 'metadata'])
def test_failed_paint_preserves_previous_media_video_and_metadata(painted_client, monkeypatch, failure):
    client, project_id, path = painted_client
    assert apply(client, project_id).status_code == 200
    store = client.app.state.store
    project = store.read(project_id)
    project['render'] = {'status': 'ready', 'video_url': f'/api/projects/{project_id}/video', 'error': None}
    store.write(project)
    (path / 'video.mp4').write_bytes(b'previous-validated-video')
    previous = snapshot(path)
    if failure == 'rig':
        def fail_rig(*_args, **_kwargs):
            raise ValueError('test rig failure')
        monkeypatch.setattr('headbop.main.build_rig', fail_rig)
    elif failure == 'replace':
        original = Path.replace
        failed = False
        def fail_once(source, destination):
            nonlocal failed
            if source.name == 'rig-body.png' and source.parent.name.startswith('.paint-') and not failed:
                failed = True
                raise ValueError('test disk replacement failure')
            return original(source, destination)
        monkeypatch.setattr(Path, 'replace', fail_once)
    elif failure == 'metadata':
        def fail_write(_project):
            (path / 'project.json.tmp').write_text('interrupted-write')
            raise ValueError('test disk metadata failure')
        monkeypatch.setattr(store, 'write', fail_write)
    marks = [stroke(.1, .1, mode='erase')] if failure == 'empty' else [stroke(.4, .4)]
    response = apply(client, project_id, marks)
    assert response.status_code == 422, response.text
    assert snapshot(path) == previous
    assert not list(path.glob('.paint-*'))
    assert not store.reservations and not client.app.state.admission.users


def test_paint_is_refused_while_video_job_is_active(painted_client):
    client, project_id, path = painted_client
    previous = snapshot(path)
    client.app.state.store.active.add(project_id)
    try:
        assert apply(client, project_id).status_code == 409
        assert snapshot(path) == previous
        assert not client.app.state.store.reservations
    finally:
        client.app.state.store.active.clear()


@pytest.mark.parametrize('headers,status', [({'X-CSRF-Token': ''}, 403),
                                          ({'Origin': 'https://attacker.example'}, 403)])
def test_paint_requires_csrf_and_same_origin_before_body_validation(painted_client, headers, status):
    client, project_id, path = painted_client
    previous = snapshot(path)
    response = client.post(f'/api/projects/{project_id}/paint', content=b'invalid-json', headers=headers)
    assert response.status_code == status
    assert snapshot(path) == previous


def test_paint_rejects_excessive_declared_and_actual_body_before_processing(painted_client):
    client, project_id, path = painted_client
    previous = snapshot(path)
    endpoint = f'/api/projects/{project_id}/paint'
    response = client.post(endpoint, content=b'{}', headers={'content-length': str(256 * 1024 + 1)})
    assert response.status_code == 413
    response = client.post(endpoint, content=b' ' * (256 * 1024 + 1),
                           headers={'content-length': '1', 'content-type': 'application/json'})
    assert response.status_code == 413
    assert snapshot(path) == previous
    assert not client.app.state.store.reservations and not client.app.state.admission.users


def test_paint_validates_total_points_and_releases_reservation(painted_client):
    client, project_id, path = painted_client
    previous = snapshot(path)
    response = apply(client, project_id, [{'points': [[.5, .5]] * 1000,
                                          'radius': .02, 'mode': 'add'}] * 6)
    assert response.status_code == 422
    assert snapshot(path) == previous
    assert not client.app.state.store.reservations and not client.app.state.admission.users


@pytest.mark.parametrize('engine', ['legacy', 'v2'])
def test_painted_xml_roundtrip_preserves_mask_frames_and_engine(painted_client, engine):
    client, project_id, path = painted_client
    store = client.app.state.store
    project = store.read(project_id)
    project['settings'] = Settings(bpm=120, engine=engine, motion='smooth').model_dump()
    project['keyframes'] = frames_for(project)
    store.write(project)
    if engine == 'legacy':
        before = client.get(f'/api/projects/{project_id}/export')
        assert before.status_code == 200
        assert ET.fromstring(before.content).attrib['version'] == '1'
    painted = apply(client, project_id, [stroke(), stroke(.6, .5, mode='erase')]).json()
    original_mask = np.asarray(Image.open(path / 'foreground.png')).copy()
    exported = client.get(f'/api/projects/{project_id}/export')
    assert exported.status_code == 200
    assert ET.fromstring(exported.content).attrib['version'] == '2'
    restored = client.post('/api/import', files={'file': ('painted.xml', exported.content, 'application/xml')})
    assert restored.status_code == 201, restored.text
    imported = restored.json()
    assert imported['settings']['engine'] == engine
    assert imported['keyframes'] == painted['keyframes']
    assert imported['segmentation']['method'] == 'smart-brush'
    mask = np.asarray(Image.open(io.BytesIO(client.get(imported['foreground_url']).content)))
    assert np.array_equal(mask, original_mask)
    assert client.post(f'/api/projects/{imported["id"]}/paint', json={
        'strokes': [stroke(.4, .5, mode='erase')], 'smart': False, 'base': 'current'}).status_code == 200


def test_real_render_after_paint_generates_complete_video(painted_client):
    client, project_id, _ = painted_client
    assert apply(client, project_id).status_code == 200
    endpoint = f'/api/projects/{project_id}/render'
    assert client.post(endpoint).status_code == 202
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.get(endpoint).json()
        if result['status'] in {'ready', 'error'}:
            break
        assert result['progress'] < 100
        time.sleep(.1)
    assert result['status'] == 'ready', result
    assert result['progress'] == 100
    video = client.get(result['video_url'])
    assert video.status_code == 200 and video.content[4:8] == b'ftyp'
    assert not client.app.state.store.active and not client.app.state.store.reservations
