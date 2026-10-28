"""Scratch entries the prune cannot delete as this user: remembered once, skipped until fixed.

A container that ran as root and wrote into a bind mount under scratch (the Docker terminal
backend without ``docker_run_as_host_user``, a test or review lane's ``docker run -v``) leaves
files this user cannot unlink, or directories it cannot even read. ``rmtree(ignore_errors=True)``
removes what it can, the entry stays idle, and every hourly pass re-walked the whole subtree and
logged the same "could not fully remove" line again (218 times per entry on one host). Here such
an entry is recorded with the directories that refused access, the user gets ONE line naming the
command that clears it, and later passes skip it before walking it. A record is dropped (and
the entry judged normally again) when the entry is gone or its top directory changed, when
every recorded blocking directory is accessible now (``chown``/``chmod`` of a nested dir), when
it was made by another uid, or after :data:`RECHECK_AFTER_SECONDS` in any case. Nothing is
escalated or shelled out to: the hint is for the user to run.

POSIX only: without ``os.getuid`` (Windows) a PermissionError is usually a transient open
handle, so the prune keeps retrying there exactly as before.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

STUCK_STATE_NAME = ".prune_stuck.json"
STUCK_LOCK_NAME = STUCK_STATE_NAME + ".lock"
# Bounded retry: even a record whose blockers still look inaccessible is re-judged weekly, so a
# fix this module cannot see (ACLs, a sticky bit cleared, a changed group) is never skipped forever.
RECHECK_AFTER_SECONDS = 7 * 24 * 3600
# A record keeps this many blocking dirs; checking a subset can only re-judge early, never skip longer.
_MAX_BLOCKERS = 16
_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM})


def tracking_supported() -> bool:
    return os.name == "posix" and hasattr(os, "geteuid")


def is_permission_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in _PERMISSION_ERRNOS


def _fingerprint(entry: Path) -> list[int] | None:
    try:
        st = os.lstat(entry)
    except OSError:
        return None
    # ctime too: ``sudo chown -R``/``chmod -R`` fixes the entry without touching its mtime.
    return [st.st_ino, st.st_mtime_ns, st.st_ctime_ns]


def _accessible(path: str) -> bool:
    """True when this user can now list and delete inside *path* (or it is gone)."""
    if not os.path.lexists(path):
        return True
    kw = {"effective_ids": True} if os.access in os.supports_effective_ids else {}
    return os.access(path, os.R_OK | os.W_OK | os.X_OK, **kw)


def _still_stuck(root: Path, name: str, rec: dict, now: float) -> bool:
    blockers = rec.get("blockers")
    return (
        rec.get("uid") == os.geteuid()  # windows-footgun: ok — only reached when tracking_supported()
        and now - float(rec.get("since") or 0) < RECHECK_AFTER_SECONDS
        and rec.get("fp") == _fingerprint(root / name)
        and isinstance(blockers, list) and bool(blockers)
        and not all(_accessible(b) for b in blockers if isinstance(b, str))
    )


def _read(root: Path) -> dict[str, dict]:
    # A torn or hand-edited file reads as empty: the worst case is one re-walk and one line.
    try:
        data = json.loads((root / STUCK_STATE_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _write(root: Path, state: dict[str, dict]) -> None:
    """Publish *state* via a unique temp file + ``os.replace``: a reader sees old or new, never torn."""
    path = root / STUCK_STATE_NAME
    if not state:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return
    try:
        fd, tmp = tempfile.mkstemp(dir=root, prefix=STUCK_STATE_NAME + ".", suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


@contextlib.contextmanager
def _locked(root: Path):
    """Exclusive cross-process lock around a read-merge-write of the state file. Two pruners of
    one home (separate processes past the ``.last_prune`` throttle together) otherwise overwrite
    each other's records from stale snapshots. POSIX only, like the tracking itself."""
    import fcntl

    fd = os.open(root / STUCK_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def load_skips(root: Path, *, persist: bool = True) -> frozenset[str]:
    """Names to skip this pass. Records that no longer hold (see module doc) are dropped, so
    that entry is judged normally again."""
    if not tracking_supported() or not (root / STUCK_STATE_NAME).exists():
        return frozenset()
    now = time.time()
    if not persist:
        state = _read(root)
        return frozenset(n for n, rec in state.items() if _still_stuck(root, n, rec, now))
    try:
        with _locked(root):
            state = _read(root)
            live = {n: rec for n, rec in state.items() if _still_stuck(root, n, rec, now)}
            if live != state:
                _write(root, live)
    except OSError:
        return frozenset()
    return frozenset(live)


def _restore_owned_directory(entry: Path, path: str) -> bool:
    """Restore owner rwx inside *entry*, never traversing a symlink or changing another uid's dir."""
    if not tracking_supported():
        return False
    try:
        relative = Path(os.path.abspath(path)).relative_to(entry)
    except ValueError:
        return False
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(entry, flags)
    except OSError:
        return False
    try:
        # Anchor every lookup at an open directory: a symlink swap between a path check
        # and chmod must not grant write access to a target outside the entry.
        for part in relative.parts:
            child_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o700 == 0o700:
            return False
        os.fchmod(fd, st.st_mode | 0o700)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def rmtree_collecting_permission_errors(entry: Path) -> list[str]:
    """Remove idle trees, repairing owned read-only directories before recording real blockers."""
    entry = Path(os.path.abspath(entry))
    blockers: list[str] = []
    repaired = False

    def _onexc(func, path, exc):
        nonlocal repaired
        if is_permission_error(exc):
            # Unlink/rmdir need write access on the parent, not on the read-only file.
            for directory in (path, os.path.dirname(path)):
                if _restore_owned_directory(entry, directory):
                    repaired = True
            blocker = path if func in (os.scandir, os.open, os.lstat) else os.path.dirname(path)
            if blocker not in blockers:
                blockers.append(blocker)

    # Retry the tree, not func(path): rmtree's open/scandir callbacks may require dir_fd
    # or flags we do not have. The cap also bounds retries for ACLs and concurrent writers.
    for _ in range(3):
        blockers.clear()
        repaired = False
        if sys.version_info >= (3, 12):
            shutil.rmtree(entry, onexc=_onexc)
        else:  # pragma: no cover - 3.11
            shutil.rmtree(entry, onerror=lambda f, p, info: _onexc(f, p, info[1]))
        if not repaired:
            break
    return blockers


def removal_hint(entry: Path) -> str:
    from hermes_platform.resolver import locate_command

    hint = f"sudo rm -rf {shlex.quote(os.fspath(entry))}"
    if locate_command("docker").command:
        hint += (
            f"  (or without sudo: docker run --rm -v {shlex.quote(os.fspath(entry.parent) + ':/s')}"
            f" alpine rm -rf {shlex.quote('/s/' + entry.name)})"
        )
    return hint


def record(root: Path, entry: Path, blockers: list[str], audit) -> None:
    """Remember *entry* as not removable by this user and log the one actionable line."""
    fp = _fingerprint(entry)
    if fp is None or not blockers:
        return
    rec = {"fp": fp, "since": time.time(), "uid": os.geteuid(), "blockers": blockers[:_MAX_BLOCKERS]}  # windows-footgun: ok — only reached when tracking_supported()
    with contextlib.suppress(OSError), _locked(root):
        state = _read(root)  # re-read under the lock: merge, never publish a stale snapshot
        state[entry.name] = rec
        _write(root, state)
    audit.info(
        "scratch prune: cannot remove %r: it holds files this user (uid %d) may not read or "
        "delete, usually root-owned output of a Docker container; skipping it until that is "
        "fixed (re-checked weekly). Remove it with: %s",
        os.fspath(entry), rec["uid"], removal_hint(entry),
    )


def stuck_entries(root: Path) -> list[Path]:
    """Entries the prune is currently skipping as not removable (for ``hermes doctor``)."""
    return [root / name for name in sorted(load_skips(root, persist=False))]
