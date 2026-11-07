"""Memos for ``profiles.list``'s per-profile fields, each keyed on the file(s) it derives from.

The Bots roster polls ``profiles.list`` every 5s PER CONNECTION, and two groups of fields on every
row are recomputed from disk each time even though nothing has changed:

- the **session fields** (``last_session`` / ``worker_session`` / ``canonical_session``) — a
  read-only ``state.db`` open plus a listing query plus the canonical-title lookup, per profile;
- the **ui_meta fields** (``ui_meta`` / ``ui_meta_revisions``) — a second parse of that profile's
  ``profile.yaml``, which the listing body already parsed once for description/display_name.

Both are pure functions of the files they read, so while those files have not moved there is
nothing to recompute: an idle fleet re-derived identical rows every five seconds for every bot.

Signatures carry each file's ``(mtime_ns, size, inode)`` so atomic DB replacement invalidates
alongside ordinary commits; a fresh/racy timestamp is never retained after its in-flight read.

Only DERIVED values live here. The raw readers keep their uncached contract: ``_read_profile_yaml``
is also used by the ui_meta CAS writer, which reads that document, mutates it and writes it back, and
a stale read there would overwrite a newer file.

This is a module of its own because ``methods_profiles``'s bodies are rebound onto ``server.py``'s
globals (``method_ctx.bind_module``), which COPIES module-level dicts rather than sharing them — a
cache declared there would be written to one copy and read from another.
"""
from __future__ import annotations

import copy
from concurrent.futures import Future
from pathlib import Path
import threading
from typing import Any, Callable, Optional

from utils import mtime_is_racy

# Each keyed by resolved profile path. One entry per profile the roster has painted; a removed
# profile leaves one stale entry, so a memo is dropped whole once it grows past any plausible fleet.
_SESSION_CACHE: dict[str, tuple[tuple, dict]] = {}
_UI_META_CACHE: dict[str, tuple[tuple, dict]] = {}
# The canonical gateway's rows also depend on who reads them (its owner filter): keyed by
# (profile path, reader).
_READER_SESSION_CACHE: dict[tuple, tuple[tuple, dict]] = {}
_MAX_ENTRIES = 512
_LOCK = threading.Lock()
_FLIGHTS: dict[tuple, Future] = {}
_GENERATION = 0

_STORE_FILES = ("state.db", "state.db-wal")


def _file_parts(path: Path, *, with_inode: bool = False) -> Optional[tuple]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (path.name, stat.st_mtime_ns, stat.st_size) + ((stat.st_ino,) if with_inode else ())


def _cached(cache: dict, key: str, signature: Optional[tuple], compute: Callable[[], dict],
            copier: Callable[[dict], dict]) -> dict[str, Any]:
    """Coalesce overlapping reads, including fresh/racy stores without retaining those answers."""
    with _LOCK:
        hit = cache.get(key)
        if signature is not None and hit is not None and hit[0] == signature:
            return copier(hit[1])
        generation = _GENERATION
        flight_key = (id(cache), key, signature, generation)
        future = _FLIGHTS.get(flight_key)
        leader = future is None
        if leader:
            future = _FLIGHTS[flight_key] = Future()
    if not leader:
        return copier(future.result())
    try:
        fields = compute()
        snapshot = copier(fields)
        with _LOCK:
            if signature is not None and generation == _GENERATION:
                if len(cache) >= _MAX_ENTRIES:
                    cache.clear()
                cache[key] = (signature, snapshot)
        future.set_result(snapshot)
        return fields
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _LOCK:
            _FLIGHTS.pop(flight_key, None)


def store_signature(profile_path: str | Path) -> Optional[tuple]:
    """``(name, mtime_ns, size, inode)`` per store file, or None when absent/racy.

    The pair the change watcher already trusts for ``sessions.changed``. None too while a file's
    mtime is fresh enough that a same-tick, same-size WAL rewrite could still hide behind it.
    """
    base = Path(profile_path)
    parts = [p for p in (_file_parts(base / name, with_inode=True) for name in _STORE_FILES) if p is not None]
    # Our own read-only open creates an EMPTY -wal sidecar on a store that never had one; it
    # carries no frames, so it must not read as "the store moved" on the very next poll.
    parts = [p for p in parts if not (p[0].endswith("-wal") and p[2] == 0)]
    if any(mtime_is_racy(p[1]) for p in parts):
        return None
    return tuple(parts) or None


def profile_yaml_signature(profile_dir: str | Path) -> Optional[tuple]:
    """``(name, mtime_ns, size, inode)`` of the profile's ``profile.yaml``, or None when absent."""
    return _file_parts(Path(profile_dir) / "profile.yaml", with_inode=True)


def cached_session_fields(profile_path: str | Path, compute: Callable[[], dict]) -> dict[str, Any]:
    """``compute()``'s fields, reused while the profile's session store has not moved."""
    return _cached(_SESSION_CACHE, str(profile_path), store_signature(profile_path), compute, copy.deepcopy)


def cached_reader_session_fields(profile_path: str | Path, reader: tuple,
                                 compute: Callable[[], dict]) -> dict[str, Any]:
    """``cached_session_fields`` for fields that also depend on *reader*, a hashable principal key."""
    return _cached(_READER_SESSION_CACHE, (str(profile_path), reader), store_signature(profile_path),
                   compute, copy.deepcopy)


def cached_ui_meta_fields(profile_dir: str | Path, compute: Callable[[], dict]) -> dict[str, Any]:
    """``compute()``'s fields, reused while the profile's ``profile.yaml`` has not changed.

    Copied deeply: ``ui_meta`` is a nested mapping the caller hands to a client.
    """
    return _cached(_UI_META_CACHE, str(profile_dir), profile_yaml_signature(profile_dir),
                   compute, copy.deepcopy)


def invalidate(profile_path: str | Path | None = None) -> None:
    """Drop one profile's memos, or all of them. For tests and for a caller that knows better."""
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        for cache in (_SESSION_CACHE, _UI_META_CACHE):
            if profile_path is None:
                cache.clear()
            else:
                cache.pop(str(profile_path), None)
        for key in [key for key in _READER_SESSION_CACHE if profile_path is None or key[0] == str(profile_path)]:
            _READER_SESSION_CACHE.pop(key, None)
