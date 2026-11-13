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


@pytest.mark.parametrize("target_tip", [False, True])
@pytest.mark.parametrize("stale,git_running", [(True, False), (False, False), (True, True)])
def test_safe_restore_never_plans_from_a_locked_stale_index(
    project, monkeypatch, stale, git_running, target_tip,
):
    manager = cm.CheckpointManager(enabled=True, max_snapshots=50)
    other = project / "other.md"
    other.write_text("original other\n", encoding="utf-8")
    assert manager.ensure_checkpoint(str(project), "A")
    target = manager.list_checkpoints(str(project))[0]["hash"]
    notes = project / "notes.md"
    notes.write_text("intermediate\n", encoding="utf-8")
    manager.new_turn()
    assert manager.ensure_checkpoint(str(project), "B")
    if target_tip:
        target = manager.list_checkpoints(str(project))[0]["hash"]
    index = cm._index_path(cm._store_path(), cm._project_hash(str(project)))
    lock = index.with_name(index.name + ".lock")
    lock.write_bytes(b"interrupted index write")
    if stale:
        old = time.time() - gitlock.STALE_LOCK_MIN_AGE_SECONDS - 60
        os.utime(lock, (old, old))
    monkeypatch.setattr(gitlock, "_git_proc_running", lambda: git_running)
    for path in (notes, other):
        path.write_text("latest agent write\n", encoding="utf-8")
        manager.record_agent_write(str(path))

    result = manager.restore(str(project), target, safe=True)
    if stale and not git_running:
        assert result["success"], result
        assert set(result["restored_files"]) == {"notes.md", "other.md"}
        assert notes.read_text(encoding="utf-8") == ("intermediate\n" if target_tip else "before\n")
        assert other.read_text(encoding="utf-8") == "original other\n"
    else:
        assert not result["success"], result
        assert lock.exists()
        assert notes.read_text(encoding="utf-8") == "latest agent write\n"
        assert other.read_text(encoding="utf-8") == "latest agent write\n"


@pytest.mark.parametrize("git_running", [False, True])
def test_diff_reports_current_files_or_refuses_locked_index(project, monkeypatch, git_running):
    manager = cm.CheckpointManager(enabled=True, max_snapshots=50)
    assert manager.ensure_checkpoint(str(project), "before")
    target = manager.list_checkpoints(str(project))[0]["hash"]
    index = cm._index_path(cm._store_path(), cm._project_hash(str(project)))
    lock = index.with_name(index.name + ".lock")
    lock.write_bytes(b"interrupted index write")
    old = time.time() - gitlock.STALE_LOCK_MIN_AGE_SECONDS - 60
    os.utime(lock, (old, old))
    monkeypatch.setattr(gitlock, "_git_proc_running", lambda: git_running)
    (project / "notes.md").write_text("current file\n", encoding="utf-8")

    result = manager.diff(str(project), target)
    if git_running:
        assert not result["success"], result
        assert lock.exists()
    else:
        assert result["success"], result
        assert "+current file" in result["diff"]
        assert not lock.exists()
