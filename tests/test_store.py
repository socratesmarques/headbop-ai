import os
import time

from headbop.store import Store


def test_cleanup_preserves_busy_projects_then_expires(tmp_path):
    store = Store(tmp_path)
    store.ttl = 1
    project_id, path = store.create()
    old = time.time() - 30
    os.utime(path, (old, old))
    store.cleanup()
    assert path.exists()
    store.building.discard(project_id)
    store.active.add(project_id)
    store.cleanup()
    assert path.exists()
    store.active.discard(project_id)
    store.cleanup()
    assert not path.exists()
