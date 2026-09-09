"""Bounded read-only page reuse; SQLite commits, not elapsed time, determine freshness."""

from __future__ import annotations

import atexit
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_state_common import stat_db_file_identity


@dataclass
class _DatabasePages:
    db: Any
    identity: tuple[int, int]
    version: int = -1
    pages: OrderedDict = field(default_factory=OrderedDict)
    size: int = 0

    def invalidate(self, version: int) -> None:
        self.version = version
        self.pages.clear()
        self.size = 0


class HistoryPageCache:
    """Keep at most eight read handles and 2 MiB of page data per database.

    A dedicated mode=ro connection makes data_version comparable across the short-lived
    SessionDB handles used by mobile reads. It never holds a transaction between calls.
    Cache lookup/publication are locked; the expensive history query runs outside that
    lock. A commit during the query prevents publication under an older version.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._databases: OrderedDict[Path, _DatabasePages] = OrderedDict()

    def invalidate_replaced(self, path: Path) -> None:
        """Retire old read handles before opening a replacement file's SQLite sidecars."""
        path = path.resolve()
        with self._lock:
            entry = self._databases.get(path)
            if entry is None:
                return
            if stat_db_file_identity(path) != entry.identity:
                del self._databases[path]
                entry.db.close()

    def lookup(self, db, key: tuple):
        """Return a publication token and cached rows; caller holds db._lock."""
        # A writer's uncommitted rows are visible only to that connection.
        if db._conn.in_transaction:
            return None, None
        path = Path(db.db_path).resolve()
        with self._lock:
            identity = stat_db_file_identity(path)
            # An open reader can still query the old inode after an atomic replacement.
            # Unknown identities are readable but cannot safely share cached pages.
            if identity is None or identity != db._db_file_identity:
                return None, None
            entry = self._databases.pop(path, None)
            try:
                if entry is not None and entry.identity != identity:
                    entry.db.close()
                    entry = None
                if entry is None:
                    from hermes_state import SessionDB
                    entry = _DatabasePages(SessionDB(db_path=path, read_only=True), identity)
                version = entry.db._conn.execute("PRAGMA data_version").fetchone()[0]
                # Bind the watcher to the same generation, including races while it opens.
                if entry.db._db_file_identity != identity or stat_db_file_identity(path) != identity:
                    entry.db.close()
                    return None, None
                if version != entry.version:
                    entry.invalidate(version)
                self._databases[path] = entry
                while len(self._databases) > 8:
                    self._databases.popitem(last=False)[1].db.close()
                cached = entry.pages.pop(key, None)
                if cached is not None:
                    entry.pages[key] = cached
                return (path, entry, version), None if cached is None else cached[0]
            except (OSError, sqlite3.Error, RuntimeError):
                # Caching must not turn an otherwise readable database into a failure.
                self._databases.pop(path, None)
                if entry is not None:
                    entry.db.close()
                return None, None

    def publish(self, token, key: tuple, rows: list[sqlite3.Row]) -> None:
        if token is None:
            return
        path, entry, version = token
        # Count string storage conservatively without encoding an extra copy of a large row.
        size = sum(4 * len(value) if isinstance(value, str) else
                   len(value) if isinstance(value, bytes) else 8
                   for row in rows for value in row)
        if size > 2 * 1024 * 1024:
            return  # Return every byte normally; only cache retention is bounded.
        with self._lock:
            if self._databases.get(path) is not entry:
                return
            try:
                current_version = entry.db._conn.execute("PRAGMA data_version").fetchone()[0]
                if stat_db_file_identity(path) != entry.identity or current_version != version:
                    entry.invalidate(current_version)
                    return
                prior = entry.pages.pop(key, None)
                entry.size += size - (prior[1] if prior is not None else 0)
                entry.pages[key] = (rows, size)
                while len(entry.pages) > 8 or entry.size > 2 * 1024 * 1024:
                    entry.size -= entry.pages.popitem(last=False)[1][1]
            except (OSError, sqlite3.Error, RuntimeError):
                self._databases.pop(path, None)
                entry.db.close()

    def clear(self) -> None:
        with self._lock:
            for entry in self._databases.values():
                entry.db.close()
            self._databases.clear()


history_pages = HistoryPageCache()
atexit.register(history_pages.clear)
