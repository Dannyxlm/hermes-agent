"""Dependency state of a deleted checkout is reclaimed; state anything live could still read, or
whose checkout this process merely cannot see, is kept (``pm/install_states.py``)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pm.environments
import pytest
from hermes_cli.worktree_ops import _prune_stale_worktrees
from pm.filesystem import lock_fd
from pm.install_states import collect_orphan_install_states, orphan_install_states


def _state(installs, key, project_root):
    state = installs / key
    (state / "inputs").mkdir(parents=True)
    (state / "inputs" / ".project-root").write_text(str(project_root), encoding="utf-8")
    (state / "environments" / "gen" / "venv").mkdir(parents=True)
    return state


def test_startup_prune_reclaims_only_provably_deleted_checkouts(tmp_path, monkeypatch):
    installs = tmp_path / "home" / "installs"
    repo = tmp_path / "repo"
    (repo / ".worktrees").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    live_root = tmp_path / "home" / "hermes-agent"
    live_root.mkdir(parents=True)
    live = _state(installs, "aaaa", live_root)
    gone_scratch = _state(installs, "bbbb", tmp_path / "home" / "cache" / "scratch" / "clone")
    gone_worktree = _state(installs, "cccc", repo / ".worktrees" / "removed-by-hand")
    # Pre-record state (no .project-root) has no evidence either way: never touched.
    unknown = installs / "dddd"
    (unknown / "environments").mkdir(parents=True)
    # A data root shared across a mount boundary records checkouts this process cannot see:
    # a host install from inside a container, a container install from the host.
    host_install = _state(installs, "eeee", tmp_path / "other-host-home" / ".hermes" / "hermes-agent")
    container_install = _state(installs, "ffff", tmp_path / "opt-hermes")

    assert orphan_install_states(installs) == [gone_scratch, gone_worktree]
    monkeypatch.setattr(pm.environments, "installs_root", lambda: installs)
    # No tree under .worktrees/ is old enough to prune: the reclaim must still run.
    _prune_stale_worktrees(str(repo))
    assert not gone_scratch.exists() and not gone_worktree.exists()
    assert all(p.is_dir() for p in (live, unknown, host_install, container_install))


def test_broker_worktree_orphan_requires_a_visible_repo_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    installs = tmp_path / "data" / "installs"
    repo = tmp_path / "worktrees" / "hermes-agent"
    repo.mkdir(parents=True)
    gone = _state(installs, "aaaa", repo / "deleted-task")
    unmounted = _state(installs, "bbbb", repo.parent / "missing-repo" / "task")
    elsewhere = tmp_path / "elsewhere" / "worktrees" / "repo"
    elsewhere.mkdir(parents=True)
    unknown = _state(installs, "cccc", elsewhere / "missing-task")
    live_root = repo / "live-task"
    live_root.mkdir()
    live = _state(installs, "dddd", live_root)

    # External parents are never inferred from the user's home directory.
    assert orphan_install_states(installs) == []
    from hermes_cli.config import atomic_config_write
    from hermes_constants import get_hermes_home
    info = repo.lstat()
    atomic_config_write(get_hermes_home() / "config.yaml", {
        "worktrees": {"install_state_gc_parents": [
            {"path": str(repo), "device": info.st_dev, "inode": info.st_ino}
        ]}
    })
    assert orphan_install_states(installs) == [gone]
    assert collect_orphan_install_states(installs) == [gone]
    assert not gone.exists()
    assert all(p.is_dir() for p in (unmounted, unknown, live))


@pytest.mark.parametrize("change", ["device", "inode", "symlink", "dangling", "home"])
def test_external_parent_identity_changes_keep_state(tmp_path, monkeypatch, change):
    from hermes_cli.config import atomic_config_write
    from hermes_constants import get_hermes_home

    installs = tmp_path / "data" / "installs"
    repo = tmp_path / "trees" / "repo"
    repo.mkdir(parents=True)
    root = repo / "deleted"
    state = _state(installs, "aaaa", root)
    info = repo.lstat()
    parent_path = str(repo)
    if change == "home":
        parent_path = "~/trees/repo"
        monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(RuntimeError("no home")))
    atomic_config_write(get_hermes_home() / "config.yaml", {
        "worktrees": {"install_state_gc_parents": [{
            "path": parent_path, "device": info.st_dev, "inode": info.st_ino,
        }]}
    })
    if change in ("device", "inode"):
        original = Path.lstat

        def changed(path):
            result = original(path)
            if path == repo:
                fields = list(result)
                fields[2 if change == "device" else 1] += 1
                return os.stat_result(fields)
            return result

        monkeypatch.setattr(Path, "lstat", changed)
    elif change == "symlink":
        target = repo.with_name("other")
        repo.rename(target)
        repo.symlink_to(target, target_is_directory=True)
    elif change == "dangling":
        root.symlink_to(repo / "missing")
    assert collect_orphan_install_states(installs) == []
    assert state.is_dir()


def test_held_orphan_is_kept(tmp_path):
    installs = tmp_path / "installs"
    locked = _state(installs, "dddd", tmp_path / "gone-a")
    leased = _state(installs, "eeee", tmp_path / "gone-b")
    runtime = _state(installs, "ffff", tmp_path / "gone-c")

    fds = [os.open(locked / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)]
    for lease_dir in (leased / "environments" / "gen" / ".leases",
                      runtime / "pm-runtime" / "generations" / "gen" / ".leases"):
        lease_dir.mkdir(parents=True)
        fds.append(os.open(lease_dir / "reader", os.O_CREAT | os.O_RDWR, 0o600))
    assert all(lock_fd(fd, wait=False) for fd in fds)
    try:
        assert collect_orphan_install_states(installs) == []
        assert locked.is_dir() and leased.is_dir() and runtime.is_dir()
    finally:
        for fd in fds:
            os.close(fd)
    # Locks released (owner exited): all are reclaimed on the next pass.
    assert sorted(p.name for p in collect_orphan_install_states(installs)) == ["dddd", "eeee", "ffff"]


@pytest.mark.platforms("posix")
@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores directory modes")
def test_partly_removed_orphan_keeps_its_record_until_retried(tmp_path):
    installs = tmp_path / "installs"
    state = _state(installs, "aaaa", tmp_path / "gone")
    stuck = state / "environments" / "gen" / "venv"
    (stuck / "pyvenv.cfg").write_text("", encoding="utf-8")
    stuck.chmod(0o500)  # its entries cannot be unlinked: the removal stops part-way
    try:
        assert collect_orphan_install_states(installs) == []
        assert orphan_install_states(installs) == [state]
    finally:
        stuck.chmod(0o700)
    assert collect_orphan_install_states(installs) == [state] and not state.exists()
