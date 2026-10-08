"""Content-free "your Inbox changed" silent pushes (Hermex R9 U12, KTD9).

Silent pushes are rare hints, not the freshness mechanism: the phone's background refresh reads
the read-only widget Inbox route when one arrives. A device opts in per installation, connection
and profile with ``mobile.inbox_push.register`` (its APNs device token); the lease lives in the
push store's ``subscriptions`` table as kind ``background`` with an ``inbox_hints`` row holding
an opaque scope token, the last send time and a durable trailing dirty flag. Logout
(``mobile.push.unregister``) deletes the lease and, by cascade, its hint state and queued jobs.

What qualifies, per owner profile (everything else, notably token, tool and heartbeat churn,
never does):

* a new final assistant reply in an Inbox-eligible chat (checked against the reply row id, so
  ten tool calls that only bump message counts schedule nothing);
* a run start or end in the push projection (``runs`` table);
* a pending approval, question or input added, answered, withdrawn or expired
  (``server_requests`` attention hook);
* a read, Done/archive, delete, title, pin, visibility or lineage change of an Inbox-eligible row
  (``session_change_cursor`` projection fields).

At most one hint is sent every :data:`MIN_INTERVAL_S` per lease. A change inside the window sets
the dirty flag, and the push worker sends one trailing hint when the window opens. The payload is
``{"aps": {"content-available": 1}, "hermex.inbox": {"version": 1, "scope": <token>}}``: no title,
text, profile or session ids.

Detection is process-local and in memory; scheduling and the trailing flag are durable. The work
runs on the push worker thread (:func:`process`), so the hooks themselves only record intent. The
worker also reads every leased profile's store itself each pass (a stat when nothing moved), so
replies are noticed with no client connected and for profiles this process never served.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

#: One hint per lease per window; trailing changes wait for the window to open.
MIN_INTERVAL_S = 20 * 60
KIND = "background"
SURFACE = "inbox"
_HINT_TTL_S = 3600
_LEASE_S = 30 * 86400
_REPLY_CHECK_LIMIT = 200
_TERMINAL = frozenset({"complete", "failed", "cancelled"})

_HINTS_SCHEMA = """CREATE TABLE IF NOT EXISTS inbox_hints (
 subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
 scope_token TEXT NOT NULL UNIQUE, last_sent_at REAL NOT NULL DEFAULT 0,
 dirty INTEGER NOT NULL DEFAULT 0)"""

_lock = threading.Lock()
_pending_profiles: set[str] = set()
# store path -> (profile, session ids whose activity may hide a new reply)
_pending_activity: dict[str, tuple[str, set]] = {}
_reply_marks: dict[tuple[str, str], int] = {}
_started_at = time.time()
_runs_watermark: float | None = None


def scope_key(profile: str) -> str:
    return hashlib.sha256(("inbox\0" + profile).encode()).hexdigest()


def _profile_label(value) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise ValueError("profile required")
    return value


# ── detection hooks (cheap; any thread) ──────────────────────────────────────────────────────

def note_profile(profile: str | None) -> None:
    if isinstance(profile, str) and profile:
        with _lock:
            _pending_profiles.add(profile)


def on_session_change(change) -> None:
    """``session_change_cursor`` listener: projection changes qualify now, activity is checked
    for a new reply on the worker."""
    if not change.profile:
        return
    with _lock:
        if change.inbox_changed:
            _pending_profiles.add(change.profile)
        if change.activity:
            _profile, ids = _pending_activity.setdefault(change.db_path, (change.profile, set()))
            ids.update(change.activity)


def note_runtime_session(sid: str) -> None:
    """``server_requests`` attention hook: the request's runtime belongs to one profile."""
    import sys
    server = sys.modules.get("tui_gateway.server")
    if server is None or not (session := server._sessions.get(sid)):
        return
    from hermes_constants import profile_name_for_home
    note_profile(profile_name_for_home(Path(session.get("profile_home") or server._hermes_home)))


# ── leases ────────────────────────────────────────────────────────────────────────────────────

def register(store, principal, *, installation_id, connection_id, profile, device_token, environment) -> dict:
    """Create or renew one installation/connection/profile lease. Re-registering the same token
    only extends the lease (no version bump, so an in-flight send is not treated as rotated)."""
    from .mobile_push_store import normalized_uuid
    if not isinstance(principal, str) or not principal or len(principal) > 256:
        raise ValueError("principal required")
    installation_id, connection_id = normalized_uuid(installation_id), normalized_uuid(connection_id)
    profile = _profile_label(profile)
    if (not isinstance(device_token, str) or not re.fullmatch(r"[0-9a-fA-F]{32,512}", device_token)
            or len(device_token) % 2):
        raise ValueError("invalid token")
    if environment not in {"production", "sandbox"}:
        raise ValueError("invalid delivery channel")
    now = store.clock()
    expires = now + _LEASE_S
    key = scope_key(profile)
    identity = (principal, installation_id, connection_id)
    with store.transaction() as db:
        db.execute(_HINTS_SCHEMA)
        existing = db.execute("""SELECT id FROM subscriptions WHERE principal=? AND installation_id=?
            AND connection_id=? AND scope=? AND kind=? AND activity_id=''""", (*identity, key, KIND)).fetchone()
        if existing is None and db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE principal=?", (principal,)).fetchone()[0] >= 1000:
            raise ValueError("subscription limit reached")
        sid = existing["id"] if existing else str(uuid.uuid4())
        db.execute("""INSERT INTO subscriptions
            (id, principal, installation_id, connection_id, scope, surface, profile, session_id,
             kind, activity_id, run_id, token, environment, categories, expires_at, preview_enabled, version)
            VALUES (?,?,?,?,?,?,?,'',?,'','',?,?,'["completion"]',?,0,1)
            ON CONFLICT(id) DO UPDATE SET expires_at=excluded.expires_at,
            version=CASE WHEN subscriptions.token!=excluded.token OR subscriptions.environment!=excluded.environment
                         THEN subscriptions.version+1 ELSE subscriptions.version END,
            token=excluded.token, environment=excluded.environment""",
            (sid, *identity, key, SURFACE, profile, KIND, device_token.lower(), environment, expires))
        db.execute("INSERT OR IGNORE INTO inbox_hints (subscription_id, scope_token) VALUES (?, ?)",
                   (sid, secrets.token_hex(16)))
        token = db.execute("SELECT scope_token FROM inbox_hints WHERE subscription_id=?", (sid,)).fetchone()[0]
    return {"subscription_id": sid, "expires_at": expires, "scope_token": token,
            "min_interval_s": MIN_INTERVAL_S}


# ── worker ────────────────────────────────────────────────────────────────────────────────────

def _has_table(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _leased_profiles(store, now) -> set[str]:
    with store._lock:
        if not _has_table(store._db, "inbox_hints"):
            return set()
        return {row[0] for row in store._db.execute(
            "SELECT DISTINCT profile FROM subscriptions WHERE kind=? AND expires_at>?", (KIND, now))}


def _run_profiles(store, now) -> set[str]:
    """Profiles whose push-projection run started or reached a terminal state since last pass."""
    global _runs_watermark
    with store._lock:
        if _runs_watermark is None:
            _runs_watermark = now  # never replay history on start
            return set()
        mark = _runs_watermark
        rows = store._db.execute("""SELECT profile, status, started_at, updated_at FROM runs
            WHERE updated_at>? OR started_at>?""", (mark, mark)).fetchall()
    profiles = set()
    for row in rows:
        _runs_watermark = max(_runs_watermark, row["updated_at"], row["started_at"])
        if row["started_at"] > mark or row["status"] in _TERMINAL:
            profiles.add(row["profile"])
    return profiles


def _new_reply(db_path: str, ids: list) -> bool:
    """True when one of ``ids`` has a final assistant reply this process has not seen (one indexed
    query). An unseen session counts only when its reply is newer than this process."""
    from hermes_state import SessionDB
    from .inbox_summaries import page_replies
    ids = ids[:_REPLY_CHECK_LIMIT]
    replies: dict = {}
    with SessionDB(db_path=Path(db_path), read_only=True) as db:
        replies = page_replies(db, [{"id": sid, "_lineage_ids": [sid], "last_read_at": None} for sid in ids])
    found = False
    for sid in ids:
        reply = replies.get(sid)
        if not reply:
            continue
        previous = _reply_marks.get((db_path, sid))
        _reply_marks[(db_path, sid)] = reply["row_id"]
        if previous is None:
            found = found or (reply.get("at") or 0) >= _started_at
        elif previous != reply["row_id"]:
            found = True
    return found


def _enqueue(db, sub, now) -> None:
    db.execute("DELETE FROM outbox WHERE subscription_id=? AND state='pending' AND lease_until<=?", (sub["id"], now))
    payload = {"aps": {"content-available": 1}, "hermex.inbox": {"version": 1, "scope": sub["scope_token"]}}
    db.execute("""INSERT OR IGNORE INTO outbox
        (job_id, subscription_id, event_id, run_id, payload, urgent, collapse_id, expires_at, next_attempt, category)
        VALUES (?,?,?,'',?,0,?,?,?,'completion')""",
        (str(uuid.uuid4()), sub["id"], f"inbox:{uuid.uuid4().hex}", json.dumps(payload, separators=(",", ":")),
         sub["id"], min(sub["expires_at"], now + _HINT_TTL_S), now))
    db.execute("UPDATE inbox_hints SET last_sent_at=?, dirty=0 WHERE subscription_id=?", (now, sub["id"]))


_LEASES = """SELECT s.id, s.expires_at, h.scope_token, h.last_sent_at, h.dirty FROM subscriptions s
    JOIN inbox_hints h ON h.subscription_id=s.id
    WHERE s.kind='background' AND s.surface='inbox' AND s.expires_at>? AND s.token!=''"""


def schedule(store, profiles, now=None) -> int:
    """Send a hint now for each lease of ``profiles`` whose window is open; mark the rest dirty."""
    now = store.clock() if now is None else now
    sent = 0
    with store.transaction() as db:
        if not _has_table(db, "inbox_hints"):
            return 0
        for profile in sorted(profiles):
            for sub in db.execute(_LEASES + " AND s.profile=?", (now, profile)).fetchall():
                if now - sub["last_sent_at"] >= MIN_INTERVAL_S:
                    _enqueue(db, sub, now)
                    sent += 1
                else:
                    db.execute("UPDATE inbox_hints SET dirty=1 WHERE subscription_id=?", (sub["id"],))
    return sent


def flush_trailing(store, now=None) -> int:
    """Send the one trailing hint of every dirty lease whose window has opened."""
    now = store.clock() if now is None else now
    with store._lock:  # read first: the common pass has nothing due and takes no write lock
        if not _has_table(store._db, "inbox_hints") or not store._db.execute(
                _LEASES + " AND h.dirty=1 AND h.last_sent_at<=? LIMIT 1", (now, now - MIN_INTERVAL_S)).fetchone():
            return 0
    with store.transaction() as db:
        if not _has_table(db, "inbox_hints"):
            return 0
        due = db.execute(_LEASES + " AND h.dirty=1 AND h.last_sent_at<=?", (now, now - MIN_INTERVAL_S)).fetchall()
        for sub in due:
            _enqueue(db, sub, now)
    return len(due)


def _observe_leased_stores(profiles) -> None:
    """Read each leased profile's store through the shared journal (a stat when nothing moved)."""
    from . import session_change_cursor
    from .mobile_widget_inbox import profile_home
    for profile in sorted(profiles):
        try:
            session_change_cursor.refresh(profile_home(profile) / "state.db")
        except (ValueError, OSError):
            continue  # a removed or invalid profile: its lease simply never fires


def _requeue(profiles, activity) -> None:
    with _lock:
        _pending_profiles.update(profiles)
        for db_path, (profile, ids) in activity.items():
            _pending_activity.setdefault(db_path, (profile, set()))[1].update(ids)


def process(store, now=None) -> int:
    """One push-worker pass: turn recorded intent into scheduled hints. Never raises; intent
    that could not be scheduled is kept for the next pass."""
    profiles, activity = set(), {}
    try:
        now = store.clock() if now is None else now
        leased = _leased_profiles(store, now)
        _observe_leased_stores(leased)
        with _lock:
            profiles = set(_pending_profiles)
            _pending_profiles.clear()
            activity = dict(_pending_activity)
            _pending_activity.clear()
        profiles |= _run_profiles(store, now)
        for db_path, (profile, ids) in list(activity.items()):
            if profile not in leased or profile in profiles:
                activity.pop(db_path)
                continue
            try:
                if _new_reply(db_path, sorted(ids)):
                    profiles.add(profile)
                activity.pop(db_path)
            except Exception as error:  # noqa: BLE001 - one unreadable store must not drop the others
                logger.warning("Inbox reply check unavailable (%s)", type(error).__name__)
        sent = schedule(store, profiles & leased, now) if profiles & leased else 0
        profiles = set()  # scheduled: a later failure must not re-queue them
        _requeue(set(), activity)  # reply checks that failed retry next pass
        return sent + flush_trailing(store, now)
    except Exception as error:  # noqa: BLE001 - hints are best effort and must not stall deliveries
        logger.warning("Inbox hint scheduling unavailable (%s)", type(error).__name__)
        _requeue(profiles, activity)
        return 0


def reset_for_tests() -> None:
    global _runs_watermark, _started_at
    with _lock:
        _pending_profiles.clear()
        _pending_activity.clear()
        _reply_marks.clear()
        _runs_watermark = None
        _started_at = time.time()


def _arm_listeners() -> None:
    from tui_gateway import server_requests, session_change_cursor
    session_change_cursor.add_listener(on_session_change)
    server_requests.add_attention_listener(note_runtime_session)


_arm_listeners()
