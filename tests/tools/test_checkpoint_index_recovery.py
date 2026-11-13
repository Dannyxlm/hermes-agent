"""A killed snapshot must not permanently disable later checkpoints."""

import os
import time

import pytest

from hermes_cli import gitlock
from tools import checkpoint_manager as cm


@pytest.fixture()
def project(tmp_path, monkeypatch):
    workdir = tmp_path / "project"
    workdir.mkdir()
    (workdir / "notes.md").write_text("before\n", encoding="utf-8")
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    monkeypatch.setattr(cm, "selected_git_env", lambda base=None: dict(base or os.environ))
    return workdir


def test_abandoned_index_lock_recovers_without_losing_history(project, monkeypatch):
    manager = cm.CheckpointManager(enabled=True, max_snapshots=50)
    assert manager.ensure_checkpoint(str(project), "before")
    before = manager.list_checkpoints(str(project))[0]["hash"]
    index = cm._index_path(cm._store_path(), cm._project_hash(str(project)))
    lock = index.with_name(index.name + ".lock")
    lock.write_bytes(b"interrupted index write")
    old = time.time() - gitlock.STALE_LOCK_MIN_AGE_SECONDS - 60
    os.utime(lock, (old, old))
    monkeypatch.setattr(gitlock, "_git_proc_running", lambda: False)

    (project / "notes.md").write_text("after\n", encoding="utf-8")
    manager.new_turn()
    assert manager.ensure_checkpoint(str(project), "after")
    assert not lock.exists()
    snapshots = manager.list_checkpoints(str(project))
    assert before in {entry["hash"] for entry in snapshots}
    assert len(snapshots) == 2
    assert manager.restore(str(project), before, safe=False)["success"]
    assert (project / "notes.md").read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize("stale,git_running", [(False, False), (True, True)])
def test_recent_or_possibly_live_index_lock_is_preserved(project, monkeypatch, stale, git_running):
    manager = cm.CheckpointManager(enabled=True, max_snapshots=50)
    assert manager.ensure_checkpoint(str(project), "before")
    before = manager.list_checkpoints(str(project))
    index = cm._index_path(cm._store_path(), cm._project_hash(str(project)))
    lock = index.with_name(index.name + ".lock")
    lock.write_bytes(b"possibly live index write")
    if stale:
        old = time.time() - gitlock.STALE_LOCK_MIN_AGE_SECONDS - 60
        os.utime(lock, (old, old))
    monkeypatch.setattr(gitlock, "_git_proc_running", lambda: git_running)

    (project / "notes.md").write_text("after\n", encoding="utf-8")
    manager.new_turn()
    assert not manager.ensure_checkpoint(str(project), "after")
    assert lock.read_bytes() == b"possibly live index write"
    assert manager.list_checkpoints(str(project)) == before
