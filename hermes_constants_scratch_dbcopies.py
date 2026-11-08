"""Fast reclaim of the database copies a finished lane leaves in scratch.

A lane that benchmarks or migrates ``state.db`` copies it into ``cache/scratch`` (13 GB or more
each). The idle prune (``hermes_constants_scratch``) only removes an entry 24 hours after its last
write, so a speed train of three lanes left ~40 GB behind and filled the disk well before the
nightly backup. A database copy nothing uses is dead weight, so it does not wait for that day:

- the file is a SQLite database (header magic, not the name) of at least
  :data:`DB_COPY_MIN_BYTES`, not a hardlink (``st_nlink == 1``: unlinking a link frees nothing);
- nothing in its whole top-level scratch entry was written for :data:`DB_COPY_IDLE_SECONDS`;
- no process this user can inspect has its working directory inside that entry or holds any file
  in it open or mapped. A lane that still runs is that process: an open connection, a shell
  parked in the lane dir, a query running. Once the lane's processes are gone, so is its claim.

Only the database file and its ``-wal``/``-shm``/``-journal`` sidecars go. Reports and logs next
to it stay under the 24 h rule, and a short ``<name>.reclaimed`` note says what happened. The
entry's directory timestamps are put back, so the reclaim does not restart that 24 h clock.

A shared read-only snapshot that several lanes use in turn (one copy per train, not one per lane)
opts out with a :data:`HOLD_NAME` file at the top of its entry: it then follows the 24 h rule only.
"""
from __future__ import annotations

import contextlib
import logging
import os
import stat
import time
from pathlib import Path

import hermes_constants_scratch_stuck as stuck

DB_COPY_MIN_BYTES = 64 * 1024 * 1024
DB_COPY_IDLE_SECONDS = 30 * 60
HOLD_NAME = ".scratch-hold"
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
NOTE_SUFFIX = ".reclaimed"
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _idle_candidates(entry: Path, cutoff: float) -> list[tuple[str, os.stat_result]] | None:
    """Database copies in *entry* when its whole subtree is idle since *cutoff*, else ``None``
    (touched, unreadable or held). Symlinks are never followed; a freshened hardlink is not a
    write (the idle prune's rule)."""
    try:
        top = os.lstat(entry)
    except OSError:
        return None
    if top.st_mtime >= cutoff:
        return None
    if not entry.is_dir() or entry.is_symlink():
        return [(os.fspath(entry), top)] if _qualifies(os.fspath(entry), top) else []
    if os.path.lexists(entry / HOLD_NAME):
        return None
    found: list[tuple[str, os.stat_result]] = []
    stack = [os.fspath(entry)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for child in it:
                    st = child.stat(follow_symlinks=False)
                    if st.st_mtime >= cutoff and not (st.st_nlink > 1 and child.is_file(follow_symlinks=False)):
                        return None
                    if child.is_dir(follow_symlinks=False):
                        stack.append(child.path)
                    elif _qualifies(child.path, st):
                        found.append((child.path, st))
        except OSError:
            return None
    return found


def _qualifies(path: str, st: os.stat_result) -> bool:
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size >= DB_COPY_MIN_BYTES
            and not path.endswith(SIDECAR_SUFFIXES) and _is_sqlite(path))


def _paths_in_use(root: Path) -> list[str] | None:
    """Paths under *root* (resolved) that a same-user process has as cwd, holds open or has
    mapped. ``None`` when processes cannot be listed at all: nothing is then reclaimed."""
    base = os.path.realpath(os.fspath(root))
    prefix = base.rstrip(os.sep) + os.sep
    used: list[str] = []

    def keep(path: str | None) -> None:
        if path:
            path = path.removesuffix(" (deleted)")
            if path == base or path.startswith(prefix):
                used.append(path)

    if os.path.isdir("/proc/self/fd"):
        return _proc_paths(keep, used)
    try:
        import psutil
    except ImportError:
        return None
    uid = os.getuid() if hasattr(os, "getuid") else None
    try:
        procs = list(psutil.process_iter(["pid"]))
    except (psutil.Error, OSError):
        return None
    for proc in procs:
        try:
            if uid is not None and proc.uids().real != uid:
                continue
            keep(proc.cwd())
            for item in proc.open_files():
                keep(item.path)
        except (psutil.Error, OSError):
            continue
    return used


def _proc_paths(keep, used: list[str]) -> list[str] | None:
    """Linux: read ``/proc`` links without following them (psutil's ``open_files()`` stats every
    target, a cost per fd and I/O on paths that are not ours). SQLite maps its ``-shm`` (and with
    ``mmap_size`` the database), so mappings count as well as descriptors."""
    uid = os.getuid()
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    for pid in pids:
        proc = f"/proc/{pid}"
        try:
            if os.stat(proc).st_uid != uid:
                continue
            keep(os.readlink(f"{proc}/cwd"))
            with os.scandir(f"{proc}/fd") as fds:
                for fd in fds:
                    with contextlib.suppress(OSError):
                        keep(os.readlink(fd.path))
            with open(f"{proc}/maps", encoding="utf-8", errors="replace") as maps:
                for line in maps:
                    fields = line.split(None, 5)
                    if len(fields) == 6:
                        keep(fields[5].rstrip("\n"))
        except OSError:
            continue  # exited mid-scan, or not inspectable (another user's, a setuid helper)
    return used


def _entry_in_use(entry: Path, used: list[str]) -> bool:
    real = os.path.realpath(os.fspath(entry))
    return any(p == real or p.startswith(real.rstrip(os.sep) + os.sep) for p in used)


def _unchanged(path: str, seen: os.stat_result) -> bool:
    try:
        now = os.lstat(path)
    except OSError:
        return False
    return (now.st_ino, now.st_dev, now.st_size, now.st_mtime_ns, now.st_nlink) == \
        (seen.st_ino, seen.st_dev, seen.st_size, seen.st_mtime_ns, 1)


def _remove_copy(path: str, seen: os.stat_result, entry: Path, audit: logging.Logger) -> int:
    """Unlink one copy and its sidecars; return the bytes freed (0 when it changed or failed)."""
    parent = os.path.dirname(path)
    try:
        parent_st = os.lstat(parent)
    except OSError:
        return 0
    if not _unchanged(path, seen):
        audit.info("scratch prune: kept database copy %r — changed since selection", path)
        return 0
    try:
        os.unlink(path)
    except OSError as exc:
        audit.info("scratch prune: could not remove database copy %r: %s", path, exc)
        return 0
    freed = seen.st_size
    for suffix in SIDECAR_SUFFIXES:
        side = path + suffix
        with contextlib.suppress(OSError):
            st = os.lstat(side)
            if st.st_nlink == 1 and not os.path.isdir(side):
                os.unlink(side)
                freed += st.st_size
    note = path + NOTE_SUFFIX
    with contextlib.suppress(OSError):
        Path(note).write_text(
            f"Removed by the Hermes scratch prune at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}: idle "
            f"SQLite copy ({seen.st_size} bytes). No process had {os.fspath(entry)!r} open or as its "
            f"cwd and nothing in it was written for {DB_COPY_IDLE_SECONDS // 60} min. Put a "
            f"{HOLD_NAME} file in the entry to keep a shared snapshot. See logs/scratch-prune.log.\n",
            encoding="utf-8")
        os.utime(note, ns=(seen.st_atime_ns, seen.st_mtime_ns))
    # The removal touched the directory; put its times back so the 24 h idle clock of the
    # entry keeps counting from the lane's last real write.
    with contextlib.suppress(OSError):
        os.utime(parent, ns=(parent_st.st_atime_ns, parent_st.st_mtime_ns))
    audit.info("scratch prune: reclaimed idle database copy %r (%d bytes)", path, freed)
    return freed


def reclaim_idle_db_copies(root: Path, skip_names: frozenset[str], audit: logging.Logger) -> int:
    """Remove the idle, unused database copies under the top-level entries of *root* (see the
    module docstring); return the bytes freed. Entries in *skip_names* and entries already known
    to hold unreadable files are not looked at."""
    skip = skip_names | stuck.load_skips(root, persist=False) | {stuck.STUCK_STATE_NAME, stuck.STUCK_LOCK_NAME}
    cutoff = time.time() - DB_COPY_IDLE_SECONDS
    try:
        entries = [e for e in root.iterdir() if e.name not in skip]
    except OSError:
        return 0
    pending = [(e, c) for e in entries if (c := _idle_candidates(e, cutoff))]
    if not pending:
        return 0  # the common pass: no process scan at all
    used = _paths_in_use(root)
    if used is None:
        audit.info("scratch prune: cannot list processes, no database copy reclaimed this pass")
        return 0
    freed = 0
    for entry, found in pending:
        # Re-validate as late as the loop allows: a lane may have come back since selection.
        if _entry_in_use(entry, used) or _idle_candidates(entry, cutoff) is None:
            audit.info("scratch prune: kept database copies in %r — in use or written since selection",
                       os.fspath(entry))
            continue
        for path, seen in found:
            freed += _remove_copy(path, seen, entry, audit)
    return freed
