"""Incremental per-session tool/skill tallies behind the dashboard Usage payload.

``InsightsEngine.get_usage_breakdown`` re-read every assistant ``tool_calls`` JSON
blob and every ``role='tool'`` row in the window on each request (about 100 MB of
JSON plus the overflow pages of large tool results on a busy state.db; ~8 s warm,
minutes with a cold page cache). Both tallies are pure functions of one session's
message rows, and almost every session in a 30-day window is already finished,
so this cache keeps them per session and re-reads only the sessions whose rows
changed.

Change detection is a fingerprint ``(message row count, max message id)`` read
through the covering ``(session_id, id)`` index, so it never touches row data.
Appends, deletes and rewinds (rows stay, ``active`` flips; the tallies count
inactive rows too, exactly like the uncached queries) all move it or leave the
tallies unchanged. The fingerprint is read before the rows, so a write that lands
in between leaves a newer tally under an older fingerprint and the next request
re-reads that session: the cache can over-refresh but never serves rows older
than its fingerprint. Residual: an in-place rewrite of an existing row's
``tool_calls``/``tool_name`` with no insert or delete in that session is not seen
until the session changes again or the process restarts.

With a ``store_path`` the tallies persist to a small JSON sidecar (counts and
tool/skill names only, never message content; mode 0600) so a dashboard restart
does not put the first Usage open back on a full scan. The sidecar records the
state.db's (st_dev, st_ino); a mismatch, a version change or any read error
discards it, and every loaded entry is still revalidated by its fingerprint.

Semantics match the uncached engine: per-session max of the two tool
representations (#9814), then summed; skill view/manage counts summed with the
latest timestamp. Tool ties are ordered by name so the payload is deterministic.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from agent.insights import _SKILL_TOOLS, _iter_functions, _parse_json

logger = logging.getLogger(__name__)

# Bound variables per statement well under SQLITE_MAX_VARIABLE_NUMBER (999 on old builds).
_CHUNK = 400

_WINDOW_ALL = (
    "SELECT s.id,"
    " (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id),"
    " (SELECT MAX(m.id) FROM messages m WHERE m.session_id = s.id)"
    " FROM sessions s WHERE s.started_at >= ?"
)
_WINDOW_WITH_SOURCE = _WINDOW_ALL + " AND s.source = ?"
# ``{marks}`` is only ever a run of ``?`` built from a length, never a value.
_TOOL_NAMES = (
    "SELECT session_id, tool_name, COUNT(*) FROM messages"
    " WHERE session_id IN ({marks}) AND role = 'tool' AND tool_name IS NOT NULL"
    " GROUP BY session_id, tool_name"
)
_ASSISTANT_CALLS = (
    "SELECT session_id, tool_calls, timestamp FROM messages"
    " WHERE session_id IN ({marks}) AND role = 'assistant' AND tool_calls IS NOT NULL"
)


class _SessionTally:
    __slots__ = ("fingerprint", "tool_names", "call_names", "skills")

    def __init__(self, fingerprint: Tuple[Any, Any]):
        self.fingerprint = fingerprint
        self.tool_names: Counter = Counter()  # role='tool' rows (gateway)
        self.call_names: Counter = Counter()  # assistant tool_calls JSON (CLI)
        self.skills: Dict[str, List[Any]] = {}  # name -> [views, manages, last_ts]


def _count_assistant_calls(tally: _SessionTally, raw_calls: Any, timestamp: Any) -> None:
    # Tool names: same tolerance as InsightsEngine._get_tool_usage (a malformed
    # entry stops that row, names already counted stay).
    try:
        for name in filter(None, (fn.get("name") for fn in _iter_functions(raw_calls))):
            tally.call_names[name] += 1
    except (TypeError, AttributeError):
        pass
    for func in _iter_functions(raw_calls):
        if not isinstance(func, dict):
            continue
        tool_name = func.get("name")
        if not isinstance(tool_name, str) or tool_name not in _SKILL_TOOLS:
            continue
        skill_name = (_parse_json(func.get("arguments"), dict) or {}).get("name")
        if not isinstance(skill_name, str) or not skill_name.strip():
            continue
        entry = tally.skills.setdefault(skill_name, [0, 0, None])
        entry[0 if tool_name == "skill_view" else 1] += 1
        if timestamp is not None and (entry[2] is None or timestamp > entry[2]):
            entry[2] = timestamp


class SessionUsageCache:
    """Per-state.db cache of per-session tool/skill tallies. Thread-safe; one
    collect runs at a time per cache, so concurrent cold requests share one scan."""

    STORE_VERSION = 1
    SAVE_INTERVAL_S = 60.0

    def __init__(self, max_entries: int = 50_000, *, store_path: Optional[str] = None,
                 db_identity: Optional[Tuple[int, int]] = None):
        self._lock = threading.Lock()
        self._entries: Dict[str, _SessionTally] = {}
        self.max_entries = max_entries
        self.last_refreshed = 0  # sessions re-read by the most recent collect
        self._store_path = store_path
        self._db_identity = list(db_identity) if db_identity else None
        self._loaded = store_path is None
        self._dirty = False
        self._saved_at = 0.0

    def collect(self, conn, cutoff: float, source: Optional[str] = None) -> Tuple[List[Dict], List[Dict]]:
        """``(tool_usage, skill_usage)`` in the shapes of ``InsightsEngine._get_tool_usage``
        and ``_get_skill_usage`` for sessions started at or after ``cutoff``."""
        with self._lock:
            if not self._loaded:
                self._loaded = True
                self._load()
            sql, params = (_WINDOW_WITH_SOURCE, (cutoff, source)) if source else (_WINDOW_ALL, (cutoff,))
            window = {row[0]: (row[1], row[2]) for row in conn.execute(sql, params).fetchall()}
            stale = [sid for sid, fp in window.items()
                     if (entry := self._entries.get(sid)) is None or entry.fingerprint != fp]
            if len(self._entries) + len(stale) > self.max_entries:
                self._entries.clear()
                stale = list(window)
            for start in range(0, len(stale), _CHUNK):
                self._tally_chunk(conn, {sid: window[sid] for sid in stale[start:start + _CHUNK]})
            self.last_refreshed = len(stale)
            if stale:
                self._dirty = True
            if self._dirty and time.monotonic() - self._saved_at >= self.SAVE_INTERVAL_S:
                self._save()
            return self._aggregate(window)

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        try:
            with open(self._store_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != self.STORE_VERSION or data.get("db") != self._db_identity:
                return
            for sid, (count, max_id, tool_names, call_names, skills) in data["entries"].items():
                tally = _SessionTally((count, max_id))
                tally.tool_names.update(dict(map(tuple, tool_names)))
                tally.call_names.update(dict(map(tuple, call_names)))
                tally.skills = {name: list(entry) for name, entry in skills}
                self._entries[sid] = tally
        except FileNotFoundError:
            return
        except Exception as exc:  # a bad sidecar only costs one full scan
            self._entries.clear()
            logger.debug("usage tally sidecar ignored (%s): %s", self._store_path, type(exc).__name__)

    def _save(self) -> None:
        self._saved_at = time.monotonic()
        if not self._store_path:
            self._dirty = False
            return
        payload = {
            "version": self.STORE_VERSION,
            "db": self._db_identity,
            "entries": {
                sid: [t.fingerprint[0], t.fingerprint[1], list(t.tool_names.items()),
                      list(t.call_names.items()), [[name, entry] for name, entry in t.skills.items()]]
                for sid, t in self._entries.items()
            },
        }
        tmp = None
        try:
            directory = os.path.dirname(self._store_path)
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".usage-tallies-", dir=directory)  # 0600
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp, self._store_path)
            tmp = None
            self._dirty = False
        except Exception as exc:  # persistence is an optimisation; never fail the request
            logger.debug("usage tally sidecar not saved (%s): %s", self._store_path, type(exc).__name__)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _tally_chunk(self, conn, chunk: Dict[str, Tuple[Any, Any]]) -> None:
        fresh = {sid: _SessionTally(fp) for sid, fp in chunk.items()}
        ids = list(chunk)
        marks = ",".join("?" * len(ids))
        for sid, name, count in conn.execute(_TOOL_NAMES.format(marks=marks), ids).fetchall():
            fresh[sid].tool_names[name] += count
        for sid, raw_calls, timestamp in conn.execute(_ASSISTANT_CALLS.format(marks=marks), ids):
            _count_assistant_calls(fresh[sid], raw_calls, timestamp)
        # Stored per chunk: an abandoned (client hung up) cold request still warms the cache.
        self._entries.update(fresh)

    def _aggregate(self, window: Dict[str, Any]) -> Tuple[List[Dict], List[Dict]]:
        tool_counts: Counter = Counter()
        skills: Dict[str, Dict[str, Any]] = {}
        for sid in window:
            tally = self._entries[sid]
            for name in tally.tool_names.keys() | tally.call_names.keys():
                tool_counts[name] += max(tally.tool_names.get(name, 0), tally.call_names.get(name, 0))
            for skill, (views, manages, last) in tally.skills.items():
                entry = skills.setdefault(skill, {"skill": skill, "view_count": 0, "manage_count": 0, "last_used_at": None})
                entry["view_count"] += views
                entry["manage_count"] += manages
                if last is not None and (entry["last_used_at"] is None or last > entry["last_used_at"]):
                    entry["last_used_at"] = last
        ranked = sorted(tool_counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
        return [{"tool_name": name, "count": count} for name, count in ranked], list(skills.values())
