"""Reclaim per-checkout dependency state whose checkout is gone.

Dependency state lives under ``<home>/installs/<install_key>`` where the key hashes the
checkout path (:func:`pm.environments.install_key`). Every ``hermes -w`` worktree, review tree
or scratch clone that boots the agent therefore commits a full environment (venv, test venv,
PM runtime). These environments can outlive a deleted checkout.

The state dir records the checkout it belongs to in ``inputs/.project-root`` (written by
:func:`pm.environments.record_activation_inputs`). A missing path alone does not prove the
checkout is gone: a data root shared with a container (``-v ~/.hermes:/opt/data``) or an
unmounted volume records paths this process cannot see. So a dir is an orphan only when its
checkout sat where this process can see that it was deleted -- under the data root that owns
``installs/`` (scratch clones, the runtime checkout), directly in a ``.worktrees/`` dir that
still exists, or in an explicitly configured parent with a matching device and inode. Removal
still fails closed on anything a live process could hold: the install
lock, or a dependency / PM runtime generation lease.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def _configured_gc_parents() -> dict[Path, tuple[int, int]]:
    """Pin opt-in external parents to filesystem identity, not just a visible name."""
    from hermes_cli.config import load_config_readonly

    parents = {}
    try:
        entries = load_config_readonly().get("worktrees", {}).get("install_state_gc_parents", [])
        if not isinstance(entries, list):
            return parents
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            raw = entry["path"]
            if raw == "~" or raw.startswith("~/"):
                path = Path.home().resolve(strict=True) / raw[2:]
            elif raw.startswith("~"):
                continue
            else:
                path = Path(raw)
            device, inode = entry.get("device"), entry.get("inode")
            if (path.is_absolute() and ".." not in path.parts
                    and type(device) is int and type(inode) is int):
                parents[path] = (device, inode)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return {}
    return parents


def _same_device_directory_chain(path: Path, anchor: Path) -> bool:
    """Reject symlinks, missing ancestors and mount transitions below the anchor."""
    try:
        device = anchor.lstat().st_dev
        for current in (path, *path.parents):
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
                return False
            if current == anchor:
                return True
    except OSError:
        return False
    return False


def _provably_deleted(root: Path, data_root: Path, parents=None) -> bool:
    if not root.is_absolute() or ".." in root.parts:
        return False
    try:
        root.lstat()  # A dangling symlink is present, not evidence of a deleted checkout.
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        return False
    if root.is_relative_to(data_root):
        # A missing scratch subtree is fine; every visible ancestor must stay on-device.
        parent = root.parent
        while parent != data_root:
            try:
                parent.lstat()
                break
            except FileNotFoundError:
                parent = parent.parent
            except OSError:
                return False
        return _same_device_directory_chain(parent, data_root)
    if root.parent.name == ".worktrees":
        return _same_device_directory_chain(root.parent, root.parent.parent)
    expected = (parents or {}).get(root.parent)
    if expected is None:
        return False
    try:
        info = root.parent.lstat()
        if (info.st_dev, info.st_ino) != expected:
            return False
    except OSError:
        return False
    return _same_device_directory_chain(root.parent, root.parent.parent)


def orphan_install_states(installs: Path) -> list[Path]:
    """State dirs whose recorded checkout is provably deleted. Dirs without a record are left alone."""
    if not installs.is_dir():
        return []
    data_root = installs.resolve().parent
    parents = _configured_gc_parents()
    orphans: list[Path] = []
    for state in sorted(installs.iterdir()):
        record = state / "inputs" / ".project-root"
        if not state.is_dir() or not record.is_file():
            continue
        try:
            root = record.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if (root and not state.is_symlink()
                and state.lstat().st_dev == data_root.lstat().st_dev
                and _provably_deleted(Path(root), data_root, parents)):
            orphans.append(state)
    return orphans


def _held(state: Path) -> bool:
    """True while a process may still be using this state dir: the install lock is taken or a
    generation lease is held. Any lock we cannot even open counts as held."""
    from hermes_cli.runtime_state import leases_held
    from pm.filesystem import lock_fd

    lock = state / ".install.lock"
    if lock.exists():
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            return True
        try:
            if not lock_fd(fd, wait=False):
                return True
        finally:
            os.close(fd)
    for generations in (state / "environments", state / "pm-runtime" / "generations"):
        if generations.is_dir():
            for generation in generations.iterdir():
                if generation.is_dir() and (generation / ".leases").is_dir() and leases_held(generation):
                    return True
    return False


def collect_orphan_install_states(installs: Path) -> list[Path]:
    """Remove every unheld orphan state dir under *installs*; returns what was removed."""
    removed: list[Path] = []
    for state in orphan_install_states(installs):
        if _held(state):
            logger.debug("orphan install state %s is still held; skipped", state.name)
            continue
        # The record goes last, and only once everything else is gone: the startup prune runs on
        # a daemon thread and a removal can fail part-way (an open file on Windows, a read-only
        # dir), and a dir that lost its record first would never be recognised again.
        for child in state.iterdir():
            if child.name == "inputs":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()
        if any(child.name != "inputs" for child in state.iterdir()):
            logger.debug("orphan install state %s only partly removed; retried next pass", state.name)
            continue
        shutil.rmtree(state, ignore_errors=True)
        if not state.exists():
            removed.append(state)
    return removed
