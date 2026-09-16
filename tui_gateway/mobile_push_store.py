"""Private device registrations and a leased, content-free SQLite delivery outbox."""

import json
import hashlib
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .mobile_push_widgets import WidgetPushStore, reader_digest

from .mobile_push_payloads import ATTENTION, LABELS, TERMINAL, Scope, activity_payload, alert_payload, generic_alert

_SCHEMA = """
CREATE TABLE IF NOT EXISTS widget_readers (
 token_hash TEXT PRIMARY KEY, principal TEXT NOT NULL, installation_id TEXT NOT NULL,
 connection_id TEXT NOT NULL, expires_at REAL NOT NULL,
 UNIQUE(principal, installation_id, connection_id));
CREATE TABLE IF NOT EXISTS subscriptions (
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, installation_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 scope TEXT NOT NULL, surface TEXT NOT NULL, profile TEXT NOT NULL, session_id TEXT NOT NULL,
 kind TEXT NOT NULL, activity_id TEXT NOT NULL, run_id TEXT NOT NULL, token TEXT NOT NULL,
 environment TEXT NOT NULL, categories TEXT NOT NULL, expires_at REAL NOT NULL, version INTEGER NOT NULL,
 preview_enabled INTEGER NOT NULL DEFAULT 0,
 last_timestamp INTEGER NOT NULL DEFAULT 0,
 UNIQUE(principal, installation_id, connection_id, scope, kind, activity_id));
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, scope TEXT NOT NULL, surface TEXT NOT NULL, profile TEXT NOT NULL,
 session_id TEXT NOT NULL, status TEXT NOT NULL, started_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS runs_scope ON runs(scope, started_at);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
 job_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 event_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL, urgent INTEGER NOT NULL,
 collapse_id TEXT NOT NULL, expires_at REAL NOT NULL, next_attempt REAL NOT NULL,
 category TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
 UNIQUE(subscription_id, event_id));
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_attempt, lease_until);
"""


def normalized_uuid(value):
    if not isinstance(value, str):
        raise ValueError("UUID required")
    return str(uuid.UUID(value))


def _validate_delivery(principal, token, environment, kind, categories):
    if not isinstance(principal, str) or not principal or len(principal) > 256:
        raise ValueError("principal required")
    if kind == "widget" and token == "":
        pass  # A read-only widget can register before WidgetKit issues its token.
    elif not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32,512}", token) or len(token) % 2:
        raise ValueError("invalid token")
    if environment not in {"production", "sandbox"} or kind not in {"alert", "activity", "widget"}:
        raise ValueError("invalid delivery channel")
    if not isinstance(categories, (list, tuple)) or any(c not in {"attention", "completion"} for c in categories):
        raise ValueError("invalid notification categories")


def _notification_category(status):
    return "attention" if status in ATTENTION or status == "failed" else "completion"


class PushStore(WidgetPushStore):
    def __init__(self, path, clock):
        self.clock = clock
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            # Serialize the additive migration across processes sharing this store.
            with self.transaction() as db:
                columns = {row["name"] for row in db.execute("PRAGMA table_info(subscriptions)")}
                if "preview_enabled" not in columns:
                    db.execute("ALTER TABLE subscriptions ADD COLUMN preview_enabled INTEGER NOT NULL DEFAULT 0")
        except sqlite3.Error:
            self._db.close()
            raise

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def register(self, principal, scope, *, installation_id, connection_id, token, environment,
                 kind="alert", categories=("attention", "completion"), activity_id="", run_id="", preview_enabled=False, read_token=None):
        if kind == "widget":
            reader_digest(read_token)
            if scope.surface != "chats":
                raise ValueError("widget requires Chats")
        installation_id, connection_id = normalized_uuid(installation_id), normalized_uuid(connection_id)
        _validate_delivery(principal, token, environment, kind, categories)
        if not isinstance(preview_enabled, bool):
            raise ValueError("preview_enabled must be a boolean")
        if kind == "activity" and (not isinstance(activity_id, str) or not 1 <= len(activity_id) <= 128):
            raise ValueError("activity identity required")
        if kind != "activity" and (activity_id or run_id):
            raise ValueError("alert registration cannot target an activity")
        now = self.clock()
        with self.transaction() as db:
            run = None
            if kind == "activity":
                run = db.execute("SELECT * FROM runs WHERE run_id=? AND scope=?", (run_id, scope.key)).fetchone()
                if run is None or run["started_at"] + 8 * 3600 - 120 <= now:
                    raise ValueError("run unavailable")
            expires = min(now + 8 * 3600, run["started_at"] + 8 * 3600) if run else now + 30 * 86400
            existing = db.execute("""SELECT id FROM subscriptions WHERE principal=? AND installation_id=?
                AND connection_id=? AND scope=? AND kind=? AND activity_id=?""",
                (principal, installation_id, connection_id, scope.key, kind, activity_id)).fetchone()
            if existing is None:
                count = db.execute("SELECT COUNT(*) FROM subscriptions WHERE principal=?", (principal,)).fetchone()[0]
                if count >= 1000:
                    raise ValueError("subscription limit reached")
            sid = existing["id"] if existing else str(uuid.uuid4())
            db.execute("""INSERT INTO subscriptions
                (id, principal, installation_id, connection_id, scope, surface, profile, session_id,
                 kind, activity_id, run_id, token, environment, categories, expires_at, preview_enabled, version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET token=excluded.token, environment=excluded.environment,
                categories=excluded.categories, run_id=excluded.run_id, expires_at=excluded.expires_at,
                preview_enabled=excluded.preview_enabled,
                version=subscriptions.version+1""", (sid, principal, installation_id, connection_id,
                scope.key, scope.surface, scope.profile, scope.session_id, kind, activity_id, run_id,
                token.lower(), environment, json.dumps(sorted(set(categories))), expires, int(preview_enabled and kind == "alert")))
            if kind == "widget":
                self._save_widget_reader(db, principal, installation_id, connection_id, read_token, expires)
            subscription = db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
            if run:
                self._enqueue(db, subscription, run, scope, f"{run_id}:registered:{subscription['version']}")
        return {"subscription_id": sid, "expires_at": expires}

    def refresh(self, principal, *, installation_id, connection_id, token, environment,
                accepts_scope, kind="alert", categories=("attention", "completion"),
                activity_id="", run_id="", preview_enabled=False):
        """Rotate existing leases without granting scopes or opening agent runtimes."""
        installation_id, connection_id = normalized_uuid(installation_id), normalized_uuid(connection_id)
        _validate_delivery(principal, token, environment, kind, categories)
        if not isinstance(preview_enabled, bool):
            raise ValueError("preview_enabled must be a boolean")
        if kind == "activity" and (not isinstance(activity_id, str) or not 1 <= len(activity_id) <= 128
                                   or not isinstance(run_id, str) or not 1 <= len(run_id) <= 256):
            raise ValueError("activity and run identity required")
        if kind == "alert" and (activity_id or run_id):
            raise ValueError("alert refresh cannot target an activity")
        now = self.clock()
        with self._lock:
            rows = self._db.execute("""SELECT * FROM subscriptions WHERE principal=? AND installation_id=?
                AND connection_id=? AND kind=? AND environment=? AND expires_at>?
                AND (?='alert' OR (activity_id=? AND run_id=?))""",
                (principal, installation_id, connection_id, kind, environment, now, kind, activity_id, run_id)).fetchall()
        # Profile/session authorization can read another DB. Do that outside our
        # transaction, then compare the selected version so logout always wins.
        accepted = [(row, Scope(row["surface"], row["profile"], row["session_id"])) for row in rows]
        accepted = [(row, scope) for row, scope in accepted if accepts_scope(scope)]
        receipts = []
        with self.transaction() as db:
            for row, scope in accepted:
                expiry = row["expires_at"] if kind == "activity" else now + 30 * 86400
                changed = db.execute("""UPDATE subscriptions SET token=?, categories=?, expires_at=?, preview_enabled=?, version=version+1
                    WHERE id=? AND version=? AND expires_at>?""",
                    (token.lower(), json.dumps(sorted(set(categories))) if kind == "alert" else row["categories"],
                     expiry, int(preview_enabled and kind == "alert"), row["id"], row["version"], self.clock())).rowcount
                if not changed:
                    continue
                sub = db.execute("SELECT * FROM subscriptions WHERE id=?", (row["id"],)).fetchone()
                if kind == "activity":
                    run = db.execute("SELECT * FROM runs WHERE run_id=? AND scope=?", (run_id, scope.key)).fetchone()
                    if run:
                        self._enqueue(db, sub, run, scope, f"{run_id}:refreshed:{sub['version']}")
                receipts.append({"subscription_id": row["id"], "expires_at": expiry})
        return {"updated": len(receipts), "subscriptions": receipts}

    def unregister(self, principal, *, installation_id, connection_id, subscription_id=None, kind=None):
        args = [principal, normalized_uuid(installation_id), normalized_uuid(connection_id)]
        query = "DELETE FROM subscriptions WHERE principal=? AND installation_id=? AND connection_id=?"
        if subscription_id is not None:
            query += " AND id=?"
            args.append(normalized_uuid(subscription_id))
        if kind is not None:
            query += " AND kind=?"
            args.append(kind)
        with self.transaction() as db:
            count = db.execute(query, args).rowcount
            if subscription_id is None and kind is None:
                db.execute("DELETE FROM widget_readers WHERE principal=? AND installation_id=? AND connection_id=?", args)
            return count

    def current_run(self, scope):
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE scope=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                                   (scope.key,)).fetchone()
            return dict(row) if row else None

    def start_run(self, scope, run_id=None, event_id=None):
        run_id = run_id or str(uuid.uuid4())
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 256:
            raise ValueError("invalid run identity")
        now = self.clock()
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None and existing["scope"] != scope.key:
                raise ValueError("run belongs to a different scope")
            db.execute("INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?,?)",
                (run_id, scope.key, scope.surface, scope.profile, scope.session_id, "starting", now, now))
        self.record(scope, run_id, event_id or f"{run_id}:start", "starting")
        with self._lock:
            return dict(self._db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def record(self, scope, run_id, event_id, status, *, preview=None):
        if status not in LABELS or not isinstance(event_id, str) or not 1 <= len(event_id) <= 512:
            raise ValueError("invalid canonical event")
        # Namespacing means a producer cannot collide with another run's request IDs.
        event_id = f"{scope.key}:{run_id}:{event_id}"
        now = self.clock()
        with self.transaction() as db:
            run = db.execute("SELECT * FROM runs WHERE run_id=? AND scope=?", (run_id, scope.key)).fetchone()
            if run is None:
                raise ValueError("run unavailable")
            if run["status"] in TERMINAL:
                return False
            if status == run["status"] and status not in ATTENTION:
                return False
            if db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (event_id, now)).rowcount == 0:
                return False
            # Repeated deltas keep the original generic status; no disk job per token.
            changed = status != run["status"]
            if run["status"] in ATTENTION and status not in ATTENTION:
                # A withdrawn/answered question must not later send an alert from a retry.
                # Already accepted Apple deliveries cannot be recalled; drop pending retries only.
                db.execute("""DELETE FROM outbox WHERE run_id=? AND state='pending'
                    AND category='attention' AND subscription_id IN
                    (SELECT id FROM subscriptions WHERE scope=? AND kind='alert')""",
                    (run_id, scope.key))
            widget_changed = changed and (run["status"] == "starting" or run["status"] in ATTENTION
                                          or status in ATTENTION or status in TERMINAL)
            updated = max(now, run["updated_at"]) if changed else run["updated_at"]
            db.execute("UPDATE runs SET status=?, updated_at=? WHERE run_id=?", (status, updated, run_id))
            run = dict(run)
            run.update(status=status, updated_at=updated)
            rows = db.execute("""SELECT * FROM subscriptions WHERE scope=? AND expires_at>?
                AND ((kind='activity' AND run_id=? AND ?) OR (kind='alert' AND ?) OR kind='widget')""",
                (scope.key, now, run_id, changed, status in ATTENTION or status in TERMINAL)).fetchall()
            for sub in rows:
                if sub["kind"] == "widget" and widget_changed:
                    self._enqueue_widget(db, sub, run, event_id)
                elif (sub["kind"] == "activity" and sub["run_id"] == run_id and changed
                        and sub["expires_at"] > now + 120):
                    self._enqueue(db, sub, run, scope, event_id, preview=preview)
                elif sub["kind"] == "alert":
                    category = _notification_category(status)
                    if (status in ATTENTION or status in TERMINAL) and category in json.loads(sub["categories"]):
                        self._enqueue(db, sub, run, scope, event_id, preview=preview)
            return True

    def _enqueue(self, db, sub, run, scope, event_id, *, preview=None):
        run = dict(run)
        now = self.clock()
        urgent = run["status"] in ATTENTION or run["status"] in TERMINAL
        activity = sub["kind"] == "activity"
        due = now if urgent else now + 10
        if run["status"] in TERMINAL:
            db.execute("""DELETE FROM outbox WHERE subscription_id=? AND run_id=?
                AND state='pending'""", (sub["id"], run["run_id"]))
        if activity:
            # The current projection supersedes every older pending activity state,
            # including attention awaiting retry. An already claimed send retains
            # its immutable timestamp, but deleting its row prevents a stale retry.
            prior_due = db.execute("""SELECT MIN(next_attempt) FROM outbox WHERE subscription_id=?
                AND run_id=? AND state='pending' AND urgent=0 AND lease_until<=?""",
                (sub["id"], run["run_id"], now)).fetchone()[0]
            if prior_due is not None:
                due = min(due, prior_due)
            db.execute("""DELETE FROM outbox WHERE subscription_id=? AND run_id=? AND state='pending'""",
                       (sub["id"], run["run_id"]))
        payload = activity_payload(run, scope, now) if activity else alert_payload(sub, run, scope, preview=preview)
        if not activity:
            payload["event_id"] = hashlib.sha256(event_id.encode()).hexdigest()
            # Optional display text must never make an otherwise valid alert too large.
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 4000:
                payload["aps"]["alert"] = generic_alert(run["status"])
        expires = min(sub["expires_at"], now + (3600 if urgent else 120))
        category = _notification_category(run["status"])
        db.execute("""INSERT OR IGNORE INTO outbox
            (job_id, subscription_id, event_id, run_id, payload, urgent, collapse_id, expires_at, next_attempt, category)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (str(uuid.uuid4()), sub["id"], event_id, run["run_id"],
            json.dumps(payload, separators=(",", ":")), int(urgent), sub["id"], expires, due, category))

    def claim(self):
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("""SELECT o.*, s.token, s.environment, s.kind, s.preview_enabled, s.version AS token_version
                FROM outbox o JOIN subscriptions s ON o.subscription_id=s.id
                WHERE o.state='pending' AND o.next_attempt<=? AND o.lease_until<=?
                AND o.expires_at>? AND s.expires_at>?
                AND (s.kind!='widget' OR s.token!='')
                AND (s.kind!='activity' OR s.last_timestamp<?)
                AND (s.kind='activity' OR EXISTS (SELECT 1 FROM json_each(s.categories) WHERE value=o.category))
                ORDER BY o.urgent DESC, o.next_attempt LIMIT 1""",
                (now, now, now, now, int(now))).fetchone()
            if row is None:
                return None
            db.execute("UPDATE outbox SET lease_until=?, attempts=attempts+1 WHERE job_id=?", (now + 30, row["job_id"]))
            job = dict(row)
            job["payload"] = json.loads(job["payload"])
            # A preference change also governs alerts that were queued earlier.
            if job["kind"] == "alert" and not job["preview_enabled"]:
                job["payload"]["aps"]["alert"] = generic_alert(job["payload"]["hermex.status"])
            job["attempts"] += 1
            if job["kind"] == "activity":
                # Order published states rather than adding future seconds for token events.
                # A second transition this second waits until the next real clock second.
                aps = job["payload"]["aps"]
                aps["timestamp"] = int(now)
                aps["dismissal-date" if aps["event"] == "end" else "stale-date"] = int(now + (300 if aps["event"] == "end" else 120))
                db.execute("UPDATE subscriptions SET last_timestamp=? WHERE id=?", (int(now), job["subscription_id"]))
            return job

    def finish(self, job, result):
        now = self.clock()
        with self.transaction() as db:
            current = db.execute("SELECT version FROM subscriptions WHERE id=?", (job["subscription_id"],)).fetchone()
            rotated = current is not None and current["version"] != job["token_version"]
            if job["kind"] == "activity" and result.outcome == "retry":
                newer = db.execute("SELECT last_timestamp FROM subscriptions WHERE id=?", (job["subscription_id"],)).fetchone()
                if newer and newer["last_timestamp"] > job["payload"]["aps"]["timestamp"]:
                    db.execute("DELETE FROM outbox WHERE job_id=?", (job["job_id"],))
                    return
            if result.outcome == "invalid":
                if job["kind"] == "widget":
                    # An invalid Apple token cannot revoke direct Inbox reads.
                    db.execute("UPDATE subscriptions SET token='', version=version+1 WHERE id=? AND version=?",
                               (job["subscription_id"], job["token_version"]))
                else:
                    db.execute("DELETE FROM subscriptions WHERE id=? AND version=?",
                               (job["subscription_id"], job["token_version"]))
            retry = (result.outcome == "retry" or (result.outcome == "invalid" and rotated)) and job["attempts"] < 8
            state = "pending" if retry else "accepted" if result.outcome == "accepted" else "failed"
            db.execute("""UPDATE outbox SET state=?, reason=?, next_attempt=?, lease_until=0
                WHERE job_id=? AND attempts=?""", (state, result.reason, now + min(900, 2 ** job["attempts"] * 5),
                job["job_id"], job["attempts"]))

    def maintain(self):
        now = self.clock()
        with self.transaction() as db:
            # Activity lifetime is not agent lifetime. End only the expiring projection;
            # long-running canonical work still gets its eventual completion alert.
            stale = db.execute("""SELECT * FROM subscriptions WHERE kind='activity'
                AND expires_at<=? AND expires_at>?""", (now + 120, now)).fetchall()
            for sub in stale:
                event_id = f"{sub['id']}:lifetime-end"
                if db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (event_id, now)).rowcount == 0:
                    continue
                run = db.execute("SELECT * FROM runs WHERE run_id=?", (sub["run_id"],)).fetchone()
                if run is not None:
                    projected = {**dict(run), "status": "cancelled", "updated_at": max(now, run["updated_at"]),
                                 "activity_expired": True}
                    self._enqueue(db, sub, projected, Scope(sub["surface"], sub["profile"], sub["session_id"]), event_id)
        with self.transaction() as db:
            db.execute("DELETE FROM widget_readers WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM subscriptions WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM outbox WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM events WHERE created_at<?", (now - 30 * 86400,))
            db.execute("DELETE FROM runs WHERE updated_at<?", (now - 30 * 86400,))

    def close(self):
        with self._lock:
            self._db.close()
