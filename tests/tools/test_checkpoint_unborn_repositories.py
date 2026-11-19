"""Real Git contracts for parent snapshots with an unborn nested repository."""
import logging
import os
import subprocess

import pytest

from tools import checkpoint_manager as cm
from utils import rmtree_readonly


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("safe", [False, True])
@pytest.mark.parametrize("size_cap", [0, 1])
@pytest.mark.parametrize("name", ["new repo [x]", "雪 repo",
                                  pytest.param("line\nbreak", marks=pytest.mark.platforms("posix"))])
@pytest.mark.parametrize("gitfile", [False, True])
def test_parent_snapshot_and_restore_preserve_uncaptured_repo(tmp_path, monkeypatch, caplog, safe, size_cap, name, gitfile):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # base = HERMES_HOME/checkpoints, resolved per call (b3f468205e)
    work = tmp_path / "work"
    nested = work / "packages" / name
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    metadata = tmp_path / "nested-git-metadata"
    if gitfile:
        (nested / ".git").rename(metadata)
        (nested / ".git").write_text(f"gitdir: {metadata}\n")
    (nested / "draft.txt").write_text("uncommitted\n")
    notes = work / "notes.txt"
    notes.write_text("before\n")
    # Exercise the same pathspec-file branch as oversize exclusion together
    # with the unborn-repository exclusion, not only its uncapped variant.
    if size_cap:
        (work / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
    before = tree_bytes(nested)
    metadata_before = tree_bytes(metadata) if gitfile else None
    mgr = cm.CheckpointManager(enabled=True, max_file_size_mb=size_cap)
    with caplog.at_level(logging.WARNING, logger=cm.__name__):
        assert mgr.ensure_checkpoint(str(work), "baseline")
        entry = mgr.list_checkpoints(str(work))[0]
        assert "uncaptured unborn repos kept" in entry["reason"]
        assert name.replace("\n", "\\n") in entry["reason"]
        notes.write_text("agent edit\n")
        mgr.record_agent_write(str(notes))
        mgr.new_turn()
        assert mgr.ensure_checkpoint(str(work), "second")
        diff = mgr.diff(str(work), entry["hash"])
        assert diff["success"] and "agent edit" in diff["diff"]
        from tools.checkpoint_undo_hint import _changed_files
        assert "packages/" + name + "/" not in _changed_files(str(work), entry["hash"])
        assert mgr.restore(str(work), entry["hash"], safe=safe)["success"]
    assert notes.read_text() == "before\n"
    assert tree_bytes(nested) == before
    if gitfile:
        assert tree_bytes(metadata) == metadata_before
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    import json
    assert json.dumps(["packages/" + name]) in warnings[0].getMessage()


@pytest.mark.parametrize("safe", [False, True])
def test_exclusion_survives_reload_and_removed_git_marker(tmp_path, monkeypatch, safe):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # base = HERMES_HOME/checkpoints, resolved per call (b3f468205e)
    work = tmp_path / "work"
    nested = work / "new"
    nested.mkdir(parents=True)
    notes = work / "notes.txt"
    notes.write_text("before\n")
    draft = nested / "draft.txt"
    draft.write_text("never captured\n")
    mgr = cm.CheckpointManager(enabled=True)
    git(nested, "init", "-q")
    assert mgr.ensure_checkpoint(str(work), "excluded from the first snapshot")
    entry = mgr.list_checkpoints(str(work))[0]
    assert "new" in entry["reason"]
    # A future snapshot can capture the path again; the old checkpoint's gap
    # remains authoritative even when no .git marker survives to rediscover it.
    rmtree_readonly(nested / ".git")
    notes.write_text("after\n")
    draft.write_text("preserve this\n")
    mgr = cm.CheckpointManager(enabled=True)
    mgr.record_agent_write(str(notes))
    mgr.record_agent_write(str(draft))
    assert mgr.ensure_checkpoint(str(work), "captured again")
    before = tree_bytes(nested)
    result = mgr.restore(str(work), entry["hash"], safe=safe)
    assert result["success"]
    assert "uncaptured unborn repos kept" in result["reason"] and '"new"' in result["reason"]
    assert notes.read_text() == "before\n"
    assert tree_bytes(nested) == before
    before_work = tree_bytes(work)
    refused = mgr.restore(str(work), entry["hash"], file_path="new/draft.txt")
    assert not refused["success"]
    assert tree_bytes(work) == before_work


@pytest.mark.parametrize("safe", [False, True])
@pytest.mark.parametrize("old_shape", ["directory", "ancestor-file"])
def test_restore_preserves_coverage_and_protects_uncaptured_ancestor_collision(tmp_path, monkeypatch, safe, old_shape):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # base = HERMES_HOME/checkpoints, resolved per call (b3f468205e)
    work = tmp_path / "work"
    work.mkdir()
    notes = work / "notes.txt"
    notes.write_text("before\n")
    parent = work / "packages"
    if old_shape == "ancestor-file":
        parent.write_text("previous file\n")
    else:
        (parent / "new").mkdir(parents=True)
        (parent / "new" / "draft.txt").write_text("previous contents\n")
    mgr = cm.CheckpointManager(enabled=True)
    assert mgr.ensure_checkpoint(str(work), "before nested init")
    target = mgr.list_checkpoints(str(work))[0]["hash"]
    if parent.is_file():
        parent.unlink()
    nested = parent / "new"
    nested.mkdir(parents=True, exist_ok=True)
    git(nested, "init", "-q")
    (nested / "draft.txt").write_text("uncaptured contents\n")
    notes.write_text("after\n")
    mgr.record_agent_write(str(notes))
    mgr.record_agent_write(str(nested / "draft.txt"))
    before = tree_bytes(nested)
    result = mgr.restore(str(work), target, safe=safe)
    assert result["success"], result
    assert notes.read_text() == "before\n"
    if old_shape == "ancestor-file":
        assert tree_bytes(nested) == before
        assert "packages/new" in result["reason"]
    else:
        # Git already captures previously indexed directories as ordinary
        # files even after git init. Do not turn that coverage into a gap.
        assert (nested / "draft.txt").read_text() == "previous contents\n"
        assert tree_bytes(nested / ".git") == {k[5:]: v for k, v in before.items() if k.startswith(".git/")}
        assert "uncaptured unborn" not in result["reason"]


@pytest.mark.parametrize("file_path", [None, "a.txt"])
def test_full_restore_repairs_corrupt_index_before_gap_inspection(tmp_path, monkeypatch, file_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # base = HERMES_HOME/checkpoints, resolved per call (b3f468205e)
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("before\n")
    mgr = cm.CheckpointManager(enabled=True)
    assert mgr.ensure_checkpoint(str(work), "baseline")
    target = mgr.list_checkpoints(str(work))[0]["hash"]
    (work / "a.txt").write_text("after\n")
    cm._index_path(cm._store_path(), cm._project_hash(str(work))).write_bytes(b"invalid index\n")
    result = mgr.restore(str(work), target, file_path=file_path)
    assert result["success"], result
    assert (work / "a.txt").read_text() == "before\n"


@pytest.mark.parametrize("safe", [False, True])
def test_previously_captured_directory_remains_recoverable_after_git_init(tmp_path, monkeypatch, safe):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # base = HERMES_HOME/checkpoints, resolved per call (b3f468205e)
    work = tmp_path / "work"
    nested = work / "app"
    nested.mkdir(parents=True)
    draft = nested / "main.py"
    draft.write_text("v1\n")
    mgr = cm.CheckpointManager(enabled=True)
    assert mgr.ensure_checkpoint(str(work), "first")
    first = mgr.list_checkpoints(str(work))[0]["hash"]
    git(nested, "init", "-q")
    draft.write_text("v2\n")
    mgr.record_agent_write(str(draft))
    mgr.new_turn()
    assert mgr.ensure_checkpoint(str(work), "second")
    second = mgr.list_checkpoints(str(work))[0]["hash"]
    assert "uncaptured unborn" not in mgr.list_checkpoints(str(work))[0]["reason"]
    comparison = mgr.diff(str(work), first)
    assert comparison["success"] and "+v2" in comparison["diff"]
    draft.write_text("v3 broken\n")
    mgr.record_agent_write(str(draft))
    metadata = tree_bytes(nested / ".git")
    assert mgr.restore(str(work), second, safe=safe)["success"]
    assert draft.read_text() == "v2\n"
    mgr.record_agent_write(str(draft))
    assert mgr.restore(str(work), first, safe=safe)["success"]
    assert draft.read_text() == "v1\n"
    assert tree_bytes(nested / ".git") == metadata


def test_listing_names_all_unborn_gaps_beside_committed_and_ignored_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # base = HERMES_HOME/checkpoints, resolved per call (b3f468205e)
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("notes\n")
    for name in ["committed-a", "committed-b", "detached"]:
        path = work / name
        path.mkdir()
        git(path, "init", "-q")
        (path / "f").write_text("committed\n")
        git(path, "add", ".")
        git(path, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "seed")
        if name == "detached":
            git(path, "checkout", "-q", "--detach")
    for name in ["unborn-a", "unborn-b", "unborn-c", "unborn-d", "node_modules/ignored"]:
        path = work / name
        path.mkdir(parents=True)
        git(path, "init", "-q")
    fake = work / "fake"
    (fake / ".git").mkdir(parents=True)
    (fake / "f").write_text("ordinary\n")
    mgr = cm.CheckpointManager(enabled=True)
    assert mgr.ensure_checkpoint(str(work), "baseline")
    entry = mgr.list_checkpoints(str(work))[0]
    for name in ["unborn-a", "unborn-b", "unborn-c", "unborn-d"]:
        assert name in entry["reason"]
    assert "ignored" not in entry["reason"] and "fake" not in entry["reason"]
    result = mgr.restore(str(work), entry["hash"])
    assert not result["success"]
    assert result["nested_repositories"] == ["committed-a", "committed-b", "detached"]
