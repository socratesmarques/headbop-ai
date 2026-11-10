"""Explicit migration preserves files and refuses ownership reassignment."""

import json

from headbop.auth import AuthStore
from headbop.manage import main
from headbop.store import Store


def test_cli_adopts_legacy_without_modifying_media_and_refuses_transfer(tmp_path, capsys):
    auth = AuthStore(tmp_path / 'security.sqlite3')
    alice = auth.create_user('alice', 'Alice-password-123')
    auth.create_user('bob', 'Bob-password-12345')
    store = Store(tmp_path)
    project_id, path = store.create()
    original = json.dumps({'id': project_id, 'settings': {'engine': 'legacy'}}).encode()
    (path / 'project.json').write_bytes(original)
    (path / 'audio.wav').write_bytes(b'legacy-media-preserved')
    args = ['--data-dir', str(tmp_path), 'projects', 'adopt', project_id]
    assert main([*args, 'alice']) == 0
    assert store.owner(project_id) == alice['id']
    assert (path / 'project.json').read_bytes() == original
    assert (path / 'audio.wav').read_bytes() == b'legacy-media-preserved'
    assert main([*args, 'bob']) == 1
    assert 'already has an owner' in capsys.readouterr().err
    assert store.owner(project_id) == alice['id']
    assert (path / 'project.json').read_bytes() == original


def test_cli_refuses_adoption_for_disabled_user_or_incomplete_project(tmp_path, capsys):
    auth = AuthStore(tmp_path / 'security.sqlite3')
    auth.create_user('alice', 'Alice-password-123')
    store = Store(tmp_path)
    project_id, path = store.create()
    args = ['--data-dir', str(tmp_path), 'projects', 'adopt', project_id, 'alice']
    assert main(args) == 1
    assert not (path / '.owner').exists()
    (path / 'project.json').write_text(json.dumps({'id': project_id}))
    auth.disable_user('alice')
    assert main(args) == 1
    assert not (path / '.owner').exists()
    assert 'active account' in capsys.readouterr().err
