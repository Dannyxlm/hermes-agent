"""Reusable inputs for the status route's 50-row activity garnish."""
from collections import OrderedDict
from pathlib import Path
from threading import Lock


_activity: OrderedDict[str, tuple[tuple, tuple]] = OrderedDict()
_lock = Lock()


def activity_signature(path: Path) -> tuple | None:
    from utils import mtime_is_racy
    path = path.resolve()
    stamps = []
    for file in (path, Path(str(path) + "-wal")):
        try:
            stat = file.stat()
        except FileNotFoundError:
            stamps.append(None)
        except OSError:
            return None
        else:
            if mtime_is_racy(stat.st_mtime_ns):
                return None
            stamps.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return str(path.resolve()), tuple(stamps)


def status_activity_times(db, path: Path, before: tuple | None) -> tuple:
    """Keep selection/compression semantics, but never cache the clock-derived count.

    The caller still opens the store through its health/schema probe on every read.
    Any DB or WAL mutation discards reuse, including receipt-only changes.
    """
    key = str(path.resolve())
    stable = before is not None and before == activity_signature(path)
    with _lock:
        cached = _activity.get(key)
        if stable and cached is not None and cached[0] == before:
            _activity.move_to_end(key)
            return cached[1]
    sessions = db.list_sessions_rich(limit=50, compact_rows=True)
    activity = tuple(s.get("last_active", s.get("started_at", 0)) for s in sessions
                     if s.get("ended_at") is None)
    if before is not None and before == activity_signature(path):
        with _lock:
            _activity[key] = before, activity
            _activity.move_to_end(key)
            while len(_activity) > 8:
                _activity.popitem(last=False)
    return activity
