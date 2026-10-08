"""Per-scope session change journal behind the ``sessions.changed`` cursor (Hermex R9 KTD5).

The change watcher already reads every ``sessions`` row of a profile store whenever the store
moves (``change_watcher._session_db_content_sig``). It hands those rows here, and this module
diffs them against the last observation to keep, per scope (one profile ``state.db``):

* an opaque cursor ``sc1.<epoch>.<scope tag>.<seq>``. The epoch is minted per process and per
  store file identity, so a restart or a replaced store invalidates every cursor;
* a bounded log of which session ids changed or were deleted at each ``seq``.

Clients that advertise ``client.capabilities {session_change_cursor: true}`` get
``sessions.changed`` frames per scope, coalesced to at most one per 250 ms, carrying the cursor
and the changed/deleted lineage roots as hints. Every other client keeps today's payload-free
frame and its 2 s floor (``change_watcher``). A list read with ``changed_since=<cursor>`` returns
only the rows of lineages touched since that cursor plus tombstones; a lost, foreign, expired or
over-large cursor answers ``repair`` and the client does one full read.

Only list-visible sessions are journaled: internal sources (kanban workers, tool runs, one-shots),
sub-agent sources and delegate child runs never wake a cursor subscriber, and a ``changed_since``
read whose filter would list them answers ``repair`` instead of a delta that could miss them.
Cron runs are journaled (Desktop lists them) but quiet: a cron-only change rides along with the
next frame instead of waking the phone, whose lists exclude cron.

In-process pending requests (approval, clarify, input) are not in the session table, so
``server_requests`` notifies :func:`note_runtime_session` when one opens or closes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

from hermes_state_common import stat_db_file_identity

logger = logging.getLogger(__name__)

#: Never journaled: their rows are not in any cursor client's list (they are listed only by
#: explicit source filters, which answer ``repair``). The listing deny-list
#: (``INTERNAL_LISTING_SOURCES``) plus sub-agent runs.
UNTRACKED_SOURCES = frozenset({"kanban", "tool", "oneshot", "subagent"})
#: Journaled, but a change touching only these never sends a cursor frame by itself.
QUIET_SOURCES = frozenset({"cron"})
# A child row with one of these sources is a delegated run, not a conversation.
_CHILD_RUN_SOURCES = frozenset({"subagent", "delegate"})
#: Inbox-eligible sources (mirrors the app's ``SessionSourcePolicy.inboxEligible``): local
#: surfaces and Hermex. Messaging platforms are display-only and never Inbox evidence.
INBOX_SOURCES = frozenset({"cli", "codex", "desktop", "gateway", "local", "tui", "webui", "mobile"})
# Row fields whose change alters the Inbox projection without a new reply: title, read marker,
# archive (Done), pin, visibility and lineage. message/tool counts are activity, not projection.
_INBOX_FIELDS = ("title", "display_name", "archived", "pinned", "hidden", "last_read_at",
                 "parent_session_id", "end_reason")

#: Cursor subscribers hear about a change within this window; everyone else keeps the 2 s floor.
CURSOR_COALESCE_S = 0.25
#: A delta touching more lineages than this answers ``repair``: one full read is cheaper.
MAX_DELTA_IDS = 200
_EVENT_ID_LIMIT = 50
_RETAIN_ENTRIES = 4096
_RETAIN_S = 6 * 3600.0
_CURSOR_PREFIX = "sc1"


def _norm_source(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


class _Row:
    __slots__ = ("full", "inbox", "source", "parent", "end_reason", "tracked")

    def __init__(self, full, inbox, source, parent, end_reason, tracked):
        self.full, self.inbox, self.source = full, inbox, source
        self.parent, self.end_reason, self.tracked = parent, end_reason, tracked

    @property
    def quiet(self) -> bool:
        return self.source in QUIET_SOURCES

    @property
    def inbox_eligible(self) -> bool:
        return self.tracked and self.source in INBOX_SOURCES


class Change:
    """One observed diff of a scope, handed to listeners (``mobile_inbox_invalidation``)."""

    __slots__ = ("db_path", "profile", "changed", "removed", "inbox_changed", "activity")

    def __init__(self, db_path, profile, changed, removed, inbox_changed, activity):
        self.db_path, self.profile = db_path, profile
        self.changed, self.removed = changed, removed
        #: Inbox-eligible ids whose projection fields changed, or which were deleted.
        self.inbox_changed = inbox_changed
        #: Inbox-eligible ids that appeared or changed only in activity fields (counts): a new
        #: reply is possible, tool churn is not proof of one.
        self.activity = activity


class _Scope:
    __slots__ = ("key", "profile", "tag", "epoch", "seq", "rows", "log", "identity",
                 "sent_seq", "sent_at", "replaced")

    def __init__(self, key: str, profile: str | None):
        self.key = key
        self.profile = profile
        self.tag = hashlib.blake2b(key.encode("utf-8", "backslashreplace"), digest_size=6).hexdigest()
        self.identity: tuple[int, int] | None = None
        self.sent_seq = 0
        self.sent_at = float("-inf")
        self.replaced = False
        self.reset()

    def reset(self) -> None:
        self.epoch = secrets.token_hex(6)
        self.seq = 0
        self.rows: dict[str, _Row] | None = None
        # (seq, changed ids, removed ids, monotonic time, loud): loud = wakes cursor subscribers
        self.log: deque = deque()
        self.sent_seq = 0

    def cursor(self) -> str:
        return f"{_CURSOR_PREFIX}.{self.epoch}.{self.tag}.{self.seq}"

    def append(self, changed: frozenset, removed: frozenset, now: float, loud: bool = True) -> None:
        self.seq += 1
        self.log.append((self.seq, changed, removed, now, loud))
        while self.log and (len(self.log) > _RETAIN_ENTRIES or now - self.log[0][3] > _RETAIN_S):
            self.log.popleft()

    def entries_after(self, seq: int):
        return [entry for entry in self.log if entry[0] > seq]

    def complete_after(self, seq: int) -> bool:
        """True when the log still holds every entry after ``seq``."""
        return seq >= self.seq or bool(self.log) and self.log[0][0] <= seq + 1

    def root_of(self, sid: str) -> str:
        return _root_in(self.rows or {}, sid)


def _root_in(rows: dict, sid: str) -> str:
    """Lineage-root hint from observed rows (compression edges only)."""
    seen = {sid}
    current = sid
    while True:
        row = rows.get(current)
        parent = row.parent if row is not None else None
        if not isinstance(parent, str) or parent in seen:
            return current
        parent_row = rows.get(parent)
        if parent_row is None or parent_row.end_reason != "compression":
            return current
        seen.add(parent)
        current = parent


_lock = threading.RLock()
_scopes: dict[str, _Scope] = {}
_subscribers: set = set()
_listeners: list[Callable[[Change], None]] = []


def scope_key(db_path) -> str:
    return os.path.realpath(os.fspath(db_path))


def _profile_for(db_path: str) -> str | None:
    try:
        from hermes_constants import profile_name_for_home
        return profile_name_for_home(Path(db_path).parent)
    except Exception:  # noqa: BLE001 - a label only; the cursor tag carries scope identity
        return None


def _scope(key: str) -> _Scope:
    scope = _scopes.get(key)
    if scope is None:
        scope = _scopes[key] = _Scope(key, _profile_for(key))
    return scope


def add_listener(listener: Callable[[Change], None]) -> None:
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def _notify(change: Change) -> None:
    for listener in list(_listeners):
        try:
            listener(change)
        except Exception:  # noqa: BLE001 - a listener must never break the watcher
            logger.debug("session change listener failed", exc_info=True)


# ── observation ───────────────────────────────────────────────────────────────────────────────

def observe(db_path, fields: tuple, observed: dict, *, delegate_ids: Callable[[list], set] | None = None,
            now: float | None = None) -> Change | None:
    """Diff one full read of a store's ``sessions`` rows against the last one.

    ``observed`` maps session id → (encoded row bytes, row values in ``fields`` order). The first
    observation of a scope seeds silently. ``delegate_ids(ids)`` classifies NEW child rows as
    delegated runs (their ``_delegate_from`` marker is not a signature field); a row's class
    never changes, so it is asked once per row."""
    if "id" not in fields:
        return None
    now = time.monotonic() if now is None else now
    index = {name: i for i, name in enumerate(fields)}

    def value(values, name):
        i = index.get(name)
        return values[i] if i is not None else None

    inbox_at = [index[name] for name in _INBOX_FIELDS if name in index]
    key = scope_key(db_path)
    identity = stat_db_file_identity(key)
    with _lock:
        scope = _scope(key)
        if scope.identity is not None and identity is not None and identity != scope.identity:
            scope.reset()  # replaced store: every outstanding cursor must repair
            scope.replaced = True
        if identity is not None:
            scope.identity = identity
        previous = scope.rows
        new_children = [sid for sid, (_e, values) in observed.items()
                        if (previous is None or sid not in previous) and value(values, "parent_session_id")]
    delegates = set()
    if new_children and delegate_ids is not None:
        try:
            delegates = set(delegate_ids(new_children))
        except Exception:  # noqa: BLE001 - unknown class: treat as visible (over-notify, never miss)
            logger.debug("delegate classification failed", exc_info=True)
    rows: dict[str, _Row] = {}
    for sid, (encoded, values) in observed.items():
        full = hash(encoded)
        old = previous.get(sid) if previous else None
        if old is not None and old.full == full:
            rows[sid] = old  # unchanged: the common case costs one hash and one lookup
            continue
        source = _norm_source(value(values, "source"))
        parent = value(values, "parent_session_id")
        if old is not None:
            tracked = old.tracked
        else:
            tracked = (source not in UNTRACKED_SOURCES
                       and not (parent and source in _CHILD_RUN_SOURCES) and sid not in delegates)
        rows[sid] = _Row(full, hash(tuple(values[i] for i in inbox_at)), source, parent,
                         value(values, "end_reason"), tracked)
    with _lock:
        if scope.rows is not previous:
            # A concurrent observation landed first; diff against it so nothing is lost.
            previous = scope.rows
        scope.rows = rows
        if previous is None:
            if scope.replaced:
                # Tell subscribers at once: their cursors belong to the old epoch and must repair.
                scope.replaced = False
                scope.append(frozenset(), frozenset(), now)
            return None
        changed, removed, inbox_changed, activity = set(), set(), set(), set()
        loud = False
        for sid, row in rows.items():
            old = previous.get(sid)
            if old is row or (old is not None and old.full == row.full):
                continue
            if not row.tracked:
                continue
            changed.add(sid)
            loud = loud or not row.quiet
            if row.inbox_eligible:
                (activity if old is None or old.inbox == row.inbox else inbox_changed).add(sid)
        for sid, old in previous.items():
            if sid not in rows and old.tracked:
                removed.add(sid)
                loud = loud or not old.quiet
                if old.inbox_eligible:
                    inbox_changed.add(sid)
                # Deleting a compression tip makes its untouched root a list row again.
                root = _root_in(previous, sid)
                if root != sid and root in rows and rows[root].tracked:
                    changed.add(root)
                    loud = loud or not rows[root].quiet
        if not changed and not removed:
            return None
        scope.append(frozenset(changed), frozenset(removed), now, loud)
        change = Change(key, scope.profile, changed, removed, inbox_changed, activity)
    _notify(change)
    return change


def note(db_path, session_ids: Iterable[str], *, now: float | None = None) -> None:
    """Record a change the session table cannot show (in-process pending requests)."""
    ids = frozenset(sid for sid in session_ids if isinstance(sid, str) and sid)
    if not ids:
        return
    with _lock:
        scope = _scopes.get(scope_key(db_path))
        if scope is None or scope.rows is None:
            return  # no cursor was ever issued for this scope; a first read is full anyway
        scope.append(ids, frozenset(), time.monotonic() if now is None else now)


def note_runtime_session(sid: str) -> None:
    """``server_requests`` attention hook: map a runtime id to its store and durable id."""
    import sys
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return
    session = server._sessions.get(sid)
    if not session:
        return
    home = Path(session.get("profile_home") or server._hermes_home)
    note(home / "state.db", [server._session_lookup_key(session, fallback=sid)])


def refresh(db_path) -> bool:
    """Bring a scope up to date before a cursor is issued or answered: the watcher's own stat-guarded
    read (a cache hit costs one stat). False when no in-process gateway can read it."""
    import sys
    server = sys.modules.get("tui_gateway.server")
    probe = getattr(server, "_session_db_content_sig", None) if server is not None else None
    if probe is None:
        return False
    try:
        probe(Path(db_path))
    except Exception:  # noqa: BLE001 - an unreadable store simply cannot issue a cursor
        logger.debug("session change refresh failed", exc_info=True)
        return False
    return True


# ── cursors ───────────────────────────────────────────────────────────────────────────────────

def current_cursor(db_path) -> str | None:
    """Cursor for the scope's latest observation; None until the scope has been read."""
    with _lock:
        scope = _scopes.get(scope_key(db_path))
        return scope.cursor() if scope is not None and scope.rows is not None else None


def _parse(cursor: Any):
    if not isinstance(cursor, str) or len(cursor) > 128:
        return None
    parts = cursor.split(".")
    if len(parts) != 4 or parts[0] != _CURSOR_PREFIX or not parts[3].isdigit():
        return None
    return parts[1], parts[2], int(parts[3])


def changes_since(db_path, cursor) -> tuple[set | None, str | None]:
    """``(touched session ids, current cursor)``; ids is None when the client must repair
    (malformed, foreign scope, other epoch, expired log, or too many changes)."""
    parsed = _parse(cursor)
    with _lock:
        scope = _scopes.get(scope_key(db_path))
        if scope is None or scope.rows is None:
            return None, None
        current = scope.cursor()
        if parsed is None:
            return None, current
        epoch, tag, seq = parsed
        if epoch != scope.epoch or tag != scope.tag or seq > scope.seq or not scope.complete_after(seq):
            return None, current
        touched: set = set()
        for _seq, changed, removed, _at, _loud in scope.entries_after(seq):
            touched |= changed
            touched |= removed
            if len(touched) > MAX_DELTA_IDS:
                return None, current
        return touched, current


def lineage_candidates(db, session_ids: Iterable[str]) -> set | None:
    """Every id of every compression lineage touching ``session_ids`` (the canonical edge rule),
    so a delta returns the lineage's projected row and tombstones every other id of it. None when
    a lineage cannot be read: a partial set would turn a live row into a tombstone."""
    candidates: set = set()
    for sid in session_ids:
        if sid in candidates:
            continue  # already on an expanded lineage's selected path
        candidates.add(sid)
        try:
            candidates.update(db.get_compression_lineage(sid) or ())
        except Exception:  # noqa: BLE001 - unreadable lineage: the caller repairs instead
            logger.debug("lineage expansion failed", exc_info=True)
            return None
    return candidates


def listing_is_tracked(*, source=None, sources=None, exclude_sources=None, include_subagents=False) -> bool:
    """Whether a list filter only admits journaled sessions, so a delta for it cannot miss a row."""
    if include_subagents:
        return False
    wanted = [_norm_source(s) for s in ([source] if source else list(sources or []))]
    if wanted:
        return not any(s in UNTRACKED_SOURCES for s in wanted)
    excluded = {_norm_source(s) for s in (exclude_sources or [])}
    return UNTRACKED_SOURCES <= excluded


def delta(db, cursor, read_rows: Callable[[list], list]) -> dict:
    """The shared ``changed_since`` answer for the REST and RPC list reads.

    ``read_rows(candidate_ids)`` lists the current rows admitted by the caller's filters among
    those ids (each lineage's root is among them, so a compressed chat comes back as its projected
    tip row). Returns ``{"repair": True, "change_cursor": None}`` or ``{"repair": False,
    "change_cursor", "rows", "tombstones"}``: tombstones are every candidate id that is no longer a
    row id of this list (deleted, archived out of the filter, or a superseded compression tip).
    The cursor is taken before the rows are read, so a change racing the read is delivered again
    by the next cursor (at least once)."""
    refresh(db.db_path)
    touched, current = changes_since(db.db_path, cursor)
    if touched is None:
        return {"repair": True, "change_cursor": None}
    candidates = lineage_candidates(db, sorted(touched)) if touched else set()
    if candidates is None:
        return {"repair": True, "change_cursor": None}
    rows = read_rows(sorted(candidates)) if candidates else []
    candidates |= {sid for row in rows for sid in (row.get("_lineage_ids") or ())}
    returned = {row.get("id") for row in rows}
    return {"repair": False, "change_cursor": current, "rows": rows,
            "tombstones": sorted(candidates - returned)}


# ── subscribers (cursor-aware WebSocket clients) ──────────────────────────────────────────────

def subscribe(transport: Any, enabled: bool) -> None:
    with _lock:
        if enabled:
            _subscribers.add(transport)
        else:
            _subscribers.discard(transport)


def is_subscribed(transport: Any) -> bool:
    with _lock:
        return transport in _subscribers


def has_subscribers() -> bool:
    with _lock:
        return bool(_subscribers)


def legacy_targets(targets: list) -> list:
    """Transports that still get the payload-free ``sessions.changed`` frame."""
    with _lock:
        if not _subscribers:
            return targets
        return [t for t in targets if t not in _subscribers]


def watch_interval(default: float) -> float:
    """The watcher's sessions probe cadence: fast only while a cursor subscriber listens."""
    return min(default, CURSOR_COALESCE_S) if has_subscribers() else default


def pump(send: Callable[[list, dict], None], live: Iterable | None = None, now: float | None = None) -> int:
    """Send each scope's pending changes to cursor subscribers, at most once per 250 ms per scope.
    ``send(transports, payload)`` writes one ``sessions.changed`` frame. Returns frames sent."""
    now = time.monotonic() if now is None else now
    frames = []
    with _lock:
        if live is not None:
            _subscribers.intersection_update(set(live))
        if not _subscribers:
            for scope in _scopes.values():
                scope.sent_seq = scope.seq  # nobody listening: nothing to replay later
            return 0
        targets = list(_subscribers)
        for scope in _scopes.values():
            if scope.rows is None or scope.seq <= scope.sent_seq:
                continue
            if now - scope.sent_at < CURSOR_COALESCE_S:
                continue
            pending = scope.entries_after(scope.sent_seq)
            if not any(entry[4] for entry in pending):
                continue  # quiet (cron-only) changes wait for the next loud one or a delta read
            changed, removed = set(), set()
            for _seq, entry_changed, entry_removed, _at, _loud in pending:
                changed |= entry_changed
                removed |= entry_removed
            roots = sorted({scope.root_of(sid) for sid in changed - removed})
            tombstoned = sorted(removed)
            payload = {"profile": scope.profile, "cursor": scope.cursor(),
                       "changed": roots[:_EVENT_ID_LIMIT], "tombstoned": tombstoned[:_EVENT_ID_LIMIT]}
            if len(roots) > _EVENT_ID_LIMIT or len(tombstoned) > _EVENT_ID_LIMIT:
                payload["truncated"] = True
            scope.sent_seq, scope.sent_at = scope.seq, now
            frames.append(payload)
    for payload in frames:
        try:
            send(targets, payload)
        except Exception:  # noqa: BLE001 - one bad write must not stop the watcher
            logger.debug("sessions.changed cursor frame failed", exc_info=True)
    return len(frames)


def reset_for_tests() -> None:
    with _lock:
        _scopes.clear()
        _subscribers.clear()


def _arm_attention_listener() -> None:
    from tui_gateway import server_requests
    server_requests.add_attention_listener(note_runtime_session)


_arm_attention_listener()
