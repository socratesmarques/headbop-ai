import os
import time

from headbop.store import Store


def test_cleanup_preserves_busy_projects_then_expires(tmp_path):
    store = Store(tmp_path)
    store.ttl = 1
    project_id, path = store.create(owner_id="a" * 32)
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


def test_ownerless_legacy_survives_cleanup_and_recovery(tmp_path):
    store = Store(tmp_path)
    store.ttl = 1
    project_id, path = store.create()
    store.write({"id": project_id, "render": {"status": "rendering"}})
    store.building.discard(project_id)
    before = (path / "project.json").read_bytes()
    os.utime(path, (1, 1))
    store.cleanup()
    store.recover()
    assert (path / "project.json").read_bytes() == before
    assert store.owner(project_id) is None


def test_atomic_reservations_cannot_overcommit_user_budget(tmp_path):
    import concurrent.futures
    import threading
    from headbop.store import QuotaError

    store = Store(tmp_path)
    store.user_storage = 100
    barrier = threading.Barrier(2)
    def reserve():
        barrier.wait()
        try:
            return store.reserve("a" * 32, 60, projects=1)
        except QuotaError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert results.count("quota_storage") == 1
    assert len(store.reservations) == 1
    store.release(next(value for value in results if value != "quota_storage"))
    assert store.reserve("a" * 32, 100)


def test_project_reservation_converts_to_owned_directory_without_double_count(tmp_path):
    import pytest
    from headbop.store import QuotaError

    store = Store(tmp_path)
    store.user_max_projects = 1
    owner = "a" * 32
    token = store.reserve(owner, 100, projects=1)
    project_id, path = store.create(owner, token)
    assert store.owner(project_id) == owner
    assert store.reservations[token]["projects"] == 0
    with pytest.raises(QuotaError, match="quota_projects"):
        store.reserve(owner, 0, projects=1)
    store.release(token)
    assert path.exists()
    # Other owners retain their own project quota.
    assert store.reserve("b" * 32, 0, projects=1)


def test_recovery_reclaims_incomplete_upload_and_interrupted_video(tmp_path):
    store = Store(tmp_path)
    abandoned, abandoned_path = store.create("a" * 32)
    project_id, path = store.create("a" * 32)
    store.write({"id": project_id, "render": {"status": "rendering"}})
    (path / "video.mp4").write_bytes(b"incomplete")
    (path / "video.mp4.partial.mp4").write_bytes(b"partial")
    store.building.clear()
    Store(tmp_path).recover()
    assert not abandoned_path.exists()
    assert not (path / "video.mp4").exists()
    assert not (path / "video.mp4.partial.mp4").exists()
    assert store.read(project_id)["render"]["status"] == "error"


def test_os_instance_lock_survives_competitors_and_releases_after_process_death(tmp_path):
    import subprocess
    import sys
    import pytest

    script = """from pathlib import Path
import sys
import time
from headbop.store import Store
with Store(Path(sys.argv[1])).instance():
    print('locked', flush=True)
    time.sleep(60)
"""
    child = subprocess.Popen([sys.executable, "-u", "-c", script, str(tmp_path)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(RuntimeError, match="one application worker"):
            with Store(tmp_path).instance():
                pass
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
    with Store(tmp_path).instance():
        assert (tmp_path / ".instance.lock").is_file()
