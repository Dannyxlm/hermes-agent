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

from .mobile_push_payloads import (
    ATTENTION, LABELS, TERMINAL, AlertPreview, Scope, activity_payload, alert_payload, generic_alert,
    redact_content_state, redact_start, start_payload)

_KINDS = frozenset({"alert", "activity", "widget", "activity_start"})
# A duplicate message.start (pre-announced continuations emit two) reuses the open run.
_DUPLICATE_START_WINDOW = 120
# In-process runs no session holds close after this idle grace; held but idle ones after the longer one.
_ORPHAN_GRACE = 120
_HELD_IDLE_GRACE = 600
_RUN_LIFETIME = 8 * 3600
# Policy-made alert leases only need to outlive one run and its questions; each run renews them.
_POLICY_LEASE = 2 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_presence (
 principal TEXT NOT NULL, installation_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 scope TEXT NOT NULL, expires_at REAL NOT NULL,
 PRIMARY KEY(principal, installation_id, connection_id, scope));
CREATE TABLE IF NOT EXISTS widget_readers (
 token_hash TEXT PRIMARY KEY, principal TEXT NOT NULL, installation_id TEXT NOT NULL,
 connection_id TEXT NOT NULL, expires_at REAL NOT NULL,
 UNIQUE(principal, installation_id, connection_id));
CREATE TABLE IF NOT EXISTS widget_inbox_grants (
 principal TEXT NOT NULL, installation_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 profile TEXT NOT NULL, home TEXT NOT NULL, grant_id TEXT NOT NULL,
 PRIMARY KEY(principal, installation_id, connection_id),
 FOREIGN KEY(principal, installation_id, connection_id)
 REFERENCES widget_readers(principal, installation_id, connection_id) ON DELETE CASCADE);
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
CREATE TABLE IF NOT EXISTS run_state (
 run_id TEXT PRIMARY KEY, owner TEXT NOT NULL DEFAULT '', closed_reason TEXT NOT NULL DEFAULT '',
 presentation TEXT NOT NULL DEFAULT '');
CREATE VIEW IF NOT EXISTS runs_ext AS SELECT r.run_id, r.scope, r.surface, r.profile, r.session_id, r.status,
 r.started_at, r.updated_at, r.rowid AS run_rowid, COALESCE(s.owner, '') AS owner,
 COALESCE(s.closed_reason, '') AS closed_reason, COALESCE(s.presentation, '') AS presentation
 FROM runs r LEFT JOIN run_state s ON s.run_id=r.run_id;
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
CREATE TABLE IF NOT EXISTS alert_policies (
 principal TEXT NOT NULL, installation_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 policy TEXT NOT NULL, token TEXT NOT NULL, environment TEXT NOT NULL, categories TEXT NOT NULL,
 preview_enabled INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL,
 PRIMARY KEY(principal, installation_id, connection_id));
CREATE TABLE IF NOT EXISTS activity_start_tokens (
 principal TEXT NOT NULL, installation_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 token TEXT NOT NULL, environment TEXT NOT NULL, expires_at REAL NOT NULL,
 PRIMARY KEY(principal, installation_id, connection_id));
CREATE TABLE IF NOT EXISTS human_origins (
 scope TEXT PRIMARY KEY, principal TEXT NOT NULL, installation_id TEXT NOT NULL,
 connection_id TEXT NOT NULL, updated_at REAL NOT NULL);
"""

# ``runs`` keeps exactly the predecessor's 8 columns: it inserts them positionally, so a
# wider table breaks every run after a rollback. Newer per-run state lives in ``run_state``
# and is read through ``runs_ext``; a run without a side row reads as legacy ('' owner).
_ADDED_COLUMNS = {
    "subscriptions": (("preview_enabled", "INTEGER NOT NULL DEFAULT 0"), ("origin", "TEXT NOT NULL DEFAULT ''")),
}
_RUN_STATE = ("owner", "closed_reason", "presentation")
_RUN_KEYS = "run_id, scope, surface, profile, session_id, status, started_at, updated_at, " + ", ".join(_RUN_STATE)
_RUNS = f"SELECT {_RUN_KEYS} FROM runs_ext"


def process_owner(pid=None):
    """``pid:start-ticks``: a recycled PID never inherits another process's runs."""
    pid = os.getpid() if pid is None else pid
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return f"{pid}:{stat.rsplit(')', 1)[1].split()[19]}"
    except (OSError, IndexError):
        return f"{pid}:"


def _owner_alive(owner):
    pid, _, started = owner.partition(":")
    if not pid.isdigit():
        return False
    if started:
        return process_owner(int(pid)) == owner
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def normalized_uuid(value):
    if not isinstance(value, str):
        raise ValueError("UUID required")
    return str(uuid.UUID(value))


def _validate_delivery(principal, token, environment, kind, categories, *, allow_missing_token=False):
    if not isinstance(principal, str) or not principal or len(principal) > 256:
        raise ValueError("principal required")
    if kind == "widget" and token == "":
        pass  # A read-only widget can register before WidgetKit issues its token.
    elif allow_missing_token and kind == "alert" and token is None:
        pass  # Existing alert leases may only narrow privacy without a fresh token.
    elif not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32,512}", token) or len(token) % 2:
        raise ValueError("invalid token")
    if environment not in {"production", "sandbox"} or kind not in _KINDS:
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
        self.owner = process_owner()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            # Serialize the additive migration across processes sharing this store.
            with self.transaction() as db:
                for table, added in _ADDED_COLUMNS.items():
                    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                    for name, declaration in added:
                        if name not in columns:
                            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                # A store the first hxr9 build widened keeps its values; the side table wins once written.
                if set(_RUN_STATE) <= {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                    db.execute("""INSERT OR IGNORE INTO run_state (run_id, owner, closed_reason, presentation)
                        SELECT run_id, owner, closed_reason, presentation FROM runs
                        WHERE owner!='' OR closed_reason!='' OR presentation!=''""")
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
        if kind == "activity_start":
            raise ValueError("push-to-start tokens use their own registration")
        if kind == "widget":
            reader_digest(read_token)
            if scope.surface not in {"chats", "native_session"}:
                raise ValueError("widget requires ordinary session")
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
                run = db.execute(f"{_RUNS} WHERE run_id=? AND scope=?", (run_id, scope.key)).fetchone()
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
                preview_enabled=excluded.preview_enabled, origin='',
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
        """Rotate existing leases; a missing alert token can only narrow their privacy."""
        installation_id, connection_id = normalized_uuid(installation_id), normalized_uuid(connection_id)
        privacy_only = kind == "alert" and token is None
        _validate_delivery(principal, token, environment, kind, categories, allow_missing_token=privacy_only)
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
                expiry = row["expires_at"] if kind == "activity" or privacy_only else now + 30 * 86400
                allowed = set(categories)
                if privacy_only:
                    allowed.intersection_update(json.loads(row["categories"]))
                preview = preview_enabled and kind == "alert" and (not privacy_only or row["preview_enabled"])
                changed = db.execute("""UPDATE subscriptions SET token=?, categories=?, expires_at=?, preview_enabled=?, version=version+1
                    WHERE id=? AND version=? AND expires_at>?""",
                    (row["token"] if privacy_only else token.lower(),
                     json.dumps(sorted(allowed)) if kind == "alert" else row["categories"],
                     expiry, int(preview), row["id"], row["version"], self.clock())).rowcount
                if not changed:
                    continue
                sub = db.execute("SELECT * FROM subscriptions WHERE id=?", (row["id"],)).fetchone()
                if kind == "activity":
                    run = db.execute(f"{_RUNS} WHERE run_id=? AND scope=?", (run_id, scope.key)).fetchone()
                    if run:
                        self._enqueue(db, sub, run, scope, f"{run_id}:refreshed:{sub['version']}")
                receipt = {"subscription_id": row["id"], "expires_at": expiry}
                if kind == "alert":
                    receipt.update(categories=json.loads(sub["categories"]), preview_enabled=bool(sub["preview_enabled"]))
                receipts.append(receipt)
            if kind == "alert":
                self._refresh_policy(db, (principal, installation_id, connection_id), environment,
                                     None if privacy_only else token.lower(), categories, preview_enabled, now)
        return {"updated": len(receipts), "subscriptions": receipts}

    def _refresh_policy(self, db, identity, environment, token, categories, preview_enabled, now):
        policy = db.execute("""SELECT * FROM alert_policies WHERE principal=? AND installation_id=?
            AND connection_id=? AND environment=? AND expires_at>?""", (*identity, environment, now)).fetchone()
        if policy is None:
            return
        allowed = set(categories)
        if token is None:  # privacy-only: narrow, never widen
            allowed.intersection_update(json.loads(policy["categories"]))
            preview_enabled = preview_enabled and bool(policy["preview_enabled"])
        self._write_policy(db, identity, policy["policy"], token or policy["token"], environment,
                           sorted(allowed), preview_enabled,
                           policy["expires_at"] if token is None else now + 30 * 86400)

    # ── Alert policy (R2): "all" listed chats or only chats this device opened ──────────────

    def set_policy(self, principal, *, installation_id, connection_id, policy, token=None, environment,
                   categories=("attention", "completion"), preview_enabled=False):
        installation_id, connection_id = normalized_uuid(installation_id), normalized_uuid(connection_id)
        identity = (principal, installation_id, connection_id)
        if policy not in {"all", "opened"}:
            raise ValueError("invalid alert policy")
        if not isinstance(preview_enabled, bool):
            raise ValueError("preview_enabled must be a boolean")
        with self.transaction() as db:
            if policy == "opened":
                db.execute("DELETE FROM alert_policies WHERE principal=? AND installation_id=? AND connection_id=?", identity)
                db.execute("""DELETE FROM subscriptions WHERE principal=? AND installation_id=? AND connection_id=?
                    AND kind='alert' AND origin='policy'""", identity)
                return {"policy": "opened", "expires_at": None}
            _validate_delivery(principal, token, environment, "alert", categories)
            expires = self.clock() + 30 * 86400
            self._write_policy(db, identity, "all", token.lower(), environment, sorted(set(categories)),
                               preview_enabled, expires)
        return {"policy": "all", "expires_at": expires, "categories": sorted(set(categories)),
                "preview_enabled": preview_enabled}

    def _write_policy(self, db, identity, policy, token, environment, categories, preview_enabled, expires):
        db.execute("""INSERT INTO alert_policies VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(principal, installation_id, connection_id) DO UPDATE SET policy=excluded.policy,
            token=excluded.token, environment=excluded.environment, categories=excluded.categories,
            preview_enabled=excluded.preview_enabled, expires_at=excluded.expires_at""",
            (*identity, policy, token, environment, json.dumps(categories), int(preview_enabled), expires))
        # Leases this policy already made follow its token and privacy; explicit leases keep theirs.
        db.execute("""UPDATE subscriptions SET token=?, environment=?, categories=?, preview_enabled=?,
            version=version+1 WHERE principal=? AND installation_id=? AND connection_id=?
            AND kind='alert' AND origin='policy'""",
            (token, environment, json.dumps(categories), int(preview_enabled), *identity))

    def policy(self, principal, *, installation_id, connection_id):
        identity = (principal, normalized_uuid(installation_id), normalized_uuid(connection_id))
        with self._lock:
            row = self._db.execute("""SELECT * FROM alert_policies WHERE principal=? AND installation_id=?
                AND connection_id=? AND expires_at>?""", (*identity, self.clock())).fetchone()
        if row is None:
            return {"policy": "opened", "expires_at": None}
        return {"policy": row["policy"], "expires_at": row["expires_at"],
                "categories": json.loads(row["categories"]), "preview_enabled": bool(row["preview_enabled"])}

    def _materialize_policy(self, db, scope, now):
        """Give every "all" device an alert lease for a listed chat as its run starts."""
        for policy in db.execute("SELECT * FROM alert_policies WHERE policy='all' AND expires_at>?", (now,)).fetchall():
            identity = (policy["principal"], policy["installation_id"], policy["connection_id"])
            expires = min(policy["expires_at"], now + _POLICY_LEASE)
            existing = db.execute("""SELECT id, origin FROM subscriptions WHERE principal=? AND installation_id=?
                AND connection_id=? AND scope=? AND kind='alert' AND activity_id=''""", (*identity, scope.key)).fetchone()
            if existing is not None:
                if existing["origin"] == "policy":
                    db.execute("UPDATE subscriptions SET expires_at=MAX(expires_at, ?) WHERE id=?", (expires, existing["id"]))
                continue  # an explicit lease for this chat keeps its own preferences
            db.execute("""INSERT INTO subscriptions
                (id, principal, installation_id, connection_id, scope, surface, profile, session_id,
                 kind, activity_id, run_id, token, environment, categories, expires_at, preview_enabled, version, origin)
                VALUES (?,?,?,?,?,?,?,?,'alert','','',?,?,?,?,?,1,'policy')""",
                (str(uuid.uuid4()), *identity, scope.key, scope.surface, scope.profile, scope.session_id,
                 policy["token"], policy["environment"], policy["categories"], expires, policy["preview_enabled"]))

    # ── Latest human origin and push-to-start tokens (KTD7, KTD8) ─────────────────────────

    def set_origin(self, scope, principal=None, *, installation_id=None, connection_id=None):
        """A phone submission records its device; any other human submission clears eligibility."""
        with self.transaction() as db:
            if principal is None:
                db.execute("DELETE FROM human_origins WHERE scope=?", (scope.key,))
                return None
            identity = (principal, normalized_uuid(installation_id), normalized_uuid(connection_id))
            db.execute("""INSERT INTO human_origins VALUES (?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET
                principal=excluded.principal, installation_id=excluded.installation_id,
                connection_id=excluded.connection_id, updated_at=excluded.updated_at""",
                (scope.key, *identity, self.clock()))
            return identity

    def origin(self, scope):
        with self._lock:
            row = self._db.execute("SELECT * FROM human_origins WHERE scope=?", (scope.key,)).fetchone()
        return dict(row) if row else None

    def register_start_token(self, principal, *, installation_id, connection_id, token, environment):
        installation_id, connection_id = normalized_uuid(installation_id), normalized_uuid(connection_id)
        _validate_delivery(principal, token, environment, "activity_start", ())
        expires = self.clock() + 30 * 86400
        with self.transaction() as db:
            db.execute("""INSERT INTO activity_start_tokens VALUES (?,?,?,?,?,?)
                ON CONFLICT(principal, installation_id, connection_id) DO UPDATE SET token=excluded.token,
                environment=excluded.environment, expires_at=excluded.expires_at""",
                (principal, installation_id, connection_id, token.lower(), environment, expires))
            # Starts queued under a rotated token must not reach Apple with the old one.
            db.execute("""DELETE FROM subscriptions WHERE principal=? AND installation_id=? AND connection_id=?
                AND kind='activity_start' AND token!=?""", (principal, installation_id, connection_id, token.lower()))
        return {"expires_at": expires}

    def unregister_start_token(self, principal, *, installation_id, connection_id):
        identity = (principal, normalized_uuid(installation_id), normalized_uuid(connection_id))
        with self.transaction() as db:
            count = db.execute("""DELETE FROM activity_start_tokens WHERE principal=? AND installation_id=?
                AND connection_id=?""", identity).rowcount
            db.execute("""DELETE FROM subscriptions WHERE principal=? AND installation_id=? AND connection_id=?
                AND kind='activity_start'""", identity)
        return count

    def _enqueue_remote_start(self, db, scope, run, preview):
        origin = db.execute("SELECT * FROM human_origins WHERE scope=?", (scope.key,)).fetchone()
        if origin is None:
            return
        identity = (origin["principal"], origin["installation_id"], origin["connection_id"])
        now = self.clock()
        start = db.execute("""SELECT * FROM activity_start_tokens WHERE principal=? AND installation_id=?
            AND connection_id=? AND expires_at>?""", (*identity, now)).fetchone()
        if start is None:
            return
        sid = str(uuid.uuid4())
        expires = run["started_at"] + _RUN_LIFETIME
        inserted = db.execute("""INSERT OR IGNORE INTO subscriptions
            (id, principal, installation_id, connection_id, scope, surface, profile, session_id,
             kind, activity_id, run_id, token, environment, categories, expires_at, preview_enabled, version, origin)
            VALUES (?,?,?,?,?,?,?,?,'activity_start',?,?,?,?,'[]',?,0,1,'start')""",
            (sid, *identity, scope.key, scope.surface, scope.profile, scope.session_id, run["run_id"],
             run["run_id"], start["token"], start["environment"], expires)).rowcount
        if not inserted:
            return  # (installation, scope, run) already has its one start
        sub = db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
        payload = start_payload(sub, dict(run), scope, now, preview=preview, previews=self._device_previews(db, sub))
        db.execute("""INSERT OR IGNORE INTO outbox
            (job_id, subscription_id, event_id, run_id, payload, urgent, collapse_id, expires_at, next_attempt, category)
            VALUES (?,?,?,?,?,1,?,?,?,'completion')""", (str(uuid.uuid4()), sid,
            f"{scope.key}:{run['run_id']}:remote-start", run["run_id"], json.dumps(payload, separators=(",", ":")),
            sid, min(expires, now + 600), now))

    def _device_previews(self, db, sub):
        """The device's current preview choice: its alert lease for this chat, else its policy."""
        identity = (sub["principal"], sub["installation_id"], sub["connection_id"])
        now = self.clock()
        lease = db.execute("""SELECT preview_enabled FROM subscriptions WHERE principal=? AND installation_id=?
            AND connection_id=? AND scope=? AND kind='alert' AND expires_at>?""",
            (*identity, sub["scope"], now)).fetchone()
        if lease is not None:
            return bool(lease["preview_enabled"])
        policy = db.execute("""SELECT preview_enabled FROM alert_policies WHERE principal=? AND installation_id=?
            AND connection_id=? AND expires_at>?""", (*identity, now)).fetchone()
        return bool(policy and policy["preview_enabled"])

    def unregister(self, principal, *, installation_id, connection_id, subscription_id=None, kind=None, surface=None):
        args = [principal, normalized_uuid(installation_id), normalized_uuid(connection_id)]
        query = "DELETE FROM subscriptions WHERE principal=? AND installation_id=? AND connection_id=?"
        if subscription_id is not None:
            query += " AND id=?"
            args.append(normalized_uuid(subscription_id))
        if kind is not None:
            query += " AND kind=?"
            args.append(kind)
        if surface is not None:
            query += " AND surface=?"
            args.append(surface)
        with self.transaction() as db:
            count = db.execute(query, args).rowcount
            if subscription_id is None and kind is None and surface is None:
                # Logout: nothing this device registered may outlive it.
                for table in ("widget_readers", "alert_policies", "activity_start_tokens", "human_origins"):
                    db.execute(f"DELETE FROM {table} WHERE principal=? AND installation_id=? AND connection_id=?", args)
            elif kind == "widget":
                if subscription_id is None:
                    db.execute("DELETE FROM widget_inbox_grants WHERE principal=? AND installation_id=? AND connection_id=?", args[:3])
                db.execute("""DELETE FROM widget_readers WHERE principal=? AND installation_id=? AND connection_id=?
                    AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.principal=widget_readers.principal
                    AND s.installation_id=widget_readers.installation_id AND s.connection_id=widget_readers.connection_id
                    AND s.kind='widget' AND s.expires_at>?)
                    AND NOT EXISTS (SELECT 1 FROM widget_inbox_grants g WHERE g.principal=widget_readers.principal
                    AND g.installation_id=widget_readers.installation_id AND g.connection_id=widget_readers.connection_id)""", (*args[:3], self.clock()))
            db.execute("""DELETE FROM mobile_presence WHERE principal=? AND installation_id=? AND connection_id=?
                AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.principal=mobile_presence.principal
                  AND s.installation_id=mobile_presence.installation_id AND s.connection_id=mobile_presence.connection_id
                  AND s.scope=mobile_presence.scope AND s.kind='alert' AND s.expires_at>?)""", (*args[:3], self.clock()))
            return count

    def presence(self, principal, scope, *, installation_id, connection_id, foreground):
        """A short, exact destination lease. Connection attachment is not app presence."""
        if scope.surface != "native_session" or not isinstance(foreground, bool):
            raise ValueError("invalid presence scope")
        identity = (principal, normalized_uuid(installation_id), normalized_uuid(connection_id), scope.key)
        now = self.clock()
        with self.transaction() as db:
            if not db.execute("""SELECT 1 FROM subscriptions WHERE principal=? AND installation_id=?
                AND connection_id=? AND scope=? AND kind='alert' AND expires_at>?""", (*identity, now)).fetchone():
                raise ValueError("registered destination required")
            db.execute("DELETE FROM mobile_presence WHERE principal=? AND installation_id=? AND connection_id=? AND scope=?", identity)
            if foreground:
                db.execute("INSERT INTO mobile_presence VALUES (?,?,?,?,?)", (*identity, now + 60))
        return {"foreground": foreground, "expires_at": now + 60 if foreground else now}

    def _foreground(self, db, sub):
        return db.execute("""SELECT 1 FROM mobile_presence WHERE principal=? AND installation_id=?
            AND connection_id=? AND scope=? AND expires_at>?""",
            (sub["principal"], sub["installation_id"], sub["connection_id"], sub["scope"], self.clock())).fetchone() is not None

    @staticmethod
    def current_run_readonly(path, scope):
        """Read an existing projection without creating a store or APNs worker."""
        from pathlib import Path
        path = Path(path).resolve()
        if not path.is_file():
            return None
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            # A store no hxr9 process has opened yet has no view: its runs are all legacy.
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='runs_ext'").fetchone() is None
            source = ("(SELECT run_id, scope, surface, profile, session_id, status, started_at, updated_at,"
                      " rowid AS run_rowid, '' AS owner, '' AS closed_reason, '' AS presentation FROM runs)"
                      if legacy else "runs_ext")
            row = db.execute(f"SELECT {_RUN_KEYS} FROM {source} WHERE scope=? ORDER BY started_at DESC, run_rowid DESC LIMIT 1",
                             (scope.key,)).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def current_run(self, scope):
        with self._lock:
            row = self._db.execute(f"{_RUNS} WHERE scope=? ORDER BY started_at DESC, run_rowid DESC LIMIT 1",
                                   (scope.key,)).fetchone()
            return dict(row) if row else None

    def start_run(self, scope, run_id=None, event_id=None, *, reuse_run_id=None, listed=False,
                  continuation=False, preview=None):
        """Open the scope's one run. A new start closes any older open run of the scope (R7).

        ``reuse_run_id`` names the caller's current run: if it is still a fresh ``starting`` run
        this start is the duplicate announcement of the same turn and yields that run.
        ``listed`` materializes "All chats" alert leases; ``continuation`` may push-start a
        Live Activity on the device whose human message is the chat's latest (KTD8).
        """
        run_id = run_id or str(uuid.uuid4())
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 256:
            raise ValueError("invalid run identity")
        now = self.clock()
        with self.transaction() as db:
            if reuse_run_id:
                reused = db.execute(f"""{_RUNS} WHERE run_id=? AND scope=? AND status='starting'
                    AND started_at>=?""", (reuse_run_id, scope.key, now - _DUPLICATE_START_WINDOW)).fetchone()
                if reused is not None:
                    return dict(reused)
            existing = db.execute(f"{_RUNS} WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None and existing["scope"] != scope.key:
                raise ValueError("run belongs to a different scope")
            inserted = db.execute("""INSERT OR IGNORE INTO runs (run_id, scope, surface, profile, session_id, status,
                started_at, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, scope.key, scope.surface, scope.profile, scope.session_id, "starting", now, now)).rowcount
            if inserted:
                # Replace, not merge: a side row left by a run the predecessor pruned is not this run's.
                db.execute("""INSERT OR REPLACE INTO run_state (run_id, owner, closed_reason, presentation)
                    VALUES (?,?,'',?)""", (run_id, self.owner, preview.to_json() if preview else ""))
            # One scope runs one turn at a time: an older open run lost its terminal event.
            for older in db.execute(f"""{_RUNS} WHERE scope=? AND run_id!=? AND status NOT IN
                    ('complete','failed','cancelled')""", (scope.key, run_id)).fetchall():
                self._close(db, older, "superseded")
            run = db.execute(f"{_RUNS} WHERE run_id=?", (run_id,)).fetchone()
            if existing is None:
                if listed:
                    self._materialize_policy(db, scope, now)
                if continuation:
                    self._enqueue_remote_start(db, scope, run, preview)
        self.record(scope, run_id, event_id or f"{run_id}:start", "starting")
        with self._lock:
            return dict(self._db.execute(f"{_RUNS} WHERE run_id=?", (run_id,)).fetchone())

    def _close(self, db, run, reason):
        """Silently close a run whose turn is gone: end its activities, never alert."""
        now = self.clock()
        updated = max(now, run["updated_at"])
        db.execute("UPDATE runs SET status='cancelled', updated_at=? WHERE run_id=?", (updated, run["run_id"]))
        self._set_run_state(db, run["run_id"], "closed_reason", reason)
        # Leased rows too: a claimed alert that comes back as "retry" must not resurface for a dead run.
        db.execute("DELETE FROM outbox WHERE run_id=? AND state='pending'", (run["run_id"],))
        closed = {**dict(run), "status": "cancelled", "closed_reason": reason, "updated_at": updated}
        scope = Scope(run["surface"], run["profile"], run["session_id"])
        event_id = f"{run['scope']}:{run['run_id']}:closed:{reason}"
        db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (event_id, now))
        for sub in db.execute("""SELECT * FROM subscriptions WHERE scope=? AND expires_at>?
                AND ((kind='activity' AND run_id=?) OR (kind='widget' AND ?))""",
                (run["scope"], now, run["run_id"], reason == "orphaned")).fetchall():
            if sub["kind"] == "widget":
                self._enqueue_widget(db, sub, closed, event_id)
            else:
                self._enqueue(db, sub, closed, scope, event_id)
        db.execute("DELETE FROM subscriptions WHERE kind='activity_start' AND run_id=?", (run["run_id"],))

    @staticmethod
    def _set_run_state(db, run_id, column, value):
        if column not in _RUN_STATE:
            raise ValueError("unknown run state")
        db.execute(f"""INSERT INTO run_state (run_id, {column}) VALUES (?,?)
            ON CONFLICT(run_id) DO UPDATE SET {column}=excluded.{column}""", (run_id, value))

    def reconcile_orphans(self, held=None, running=None, limit=200):
        """Close open runs whose turn no longer exists (R7), checked against real liveness.

        ``held``: run ids a live session of this process still points at; ``running``: the
        subset whose session is mid-turn. ``None`` means the caller cannot see sessions, so
        this process's runs are left alone. Other processes' runs close once that process is
        gone; legacy rows without an owner predate this code and its process.
        """
        now = self.clock()
        closed = 0
        with self.transaction() as db:
            rows = db.execute(f"""{_RUNS} WHERE status NOT IN ('complete','failed','cancelled')
                ORDER BY updated_at LIMIT ?""", (limit,)).fetchall()
            for run in rows:
                owner = run["owner"]
                if owner == self.owner:
                    if held is None or run["run_id"] in (running or ()):
                        continue
                    idle = now - run["updated_at"]
                    if idle < (_HELD_IDLE_GRACE if run["run_id"] in held else _ORPHAN_GRACE):
                        continue
                elif owner and _owner_alive(owner):
                    continue
                self._close(db, run, "orphaned")
                closed += 1
        return closed

    def record(self, scope, run_id, event_id, status, *, preview=None):
        if status not in LABELS or not isinstance(event_id, str) or not 1 <= len(event_id) <= 512:
            raise ValueError("invalid canonical event")
        # Namespacing means a producer cannot collide with another run's request IDs.
        event_id = f"{scope.key}:{run_id}:{event_id}"
        now = self.clock()
        presentation = preview.to_json() if preview is not None else None
        with self.transaction() as db:
            run = db.execute(f"{_RUNS} WHERE run_id=? AND scope=?", (run_id, scope.key)).fetchone()
            if run is None:
                raise ValueError("run unavailable")
            if run["status"] in TERMINAL:
                return False
            # A new step, agent count or plan progress updates only the Live Activity.
            presented = presentation is not None and presentation != run["presentation"]
            if status == run["status"] and status not in ATTENTION and not presented:
                return False
            if db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (event_id, now)).rowcount == 0:
                return False
            if presented:
                self._set_run_state(db, run_id, "presentation", presentation)
            if status in TERMINAL:
                # A push-to-start not yet delivered would open an activity for a finished turn.
                db.execute("DELETE FROM subscriptions WHERE kind='activity_start' AND run_id=?", (run_id,))
            # Repeated deltas keep the original generic status; no disk job per token.
            changed = status != run["status"] or presented
            if run["status"] in ATTENTION and status not in ATTENTION:
                # A withdrawn/answered question must not later send an alert from a retry.
                # Already accepted Apple deliveries cannot be recalled; drop pending retries only.
                db.execute("""DELETE FROM outbox WHERE run_id=? AND state='pending'
                    AND category='attention' AND subscription_id IN
                    (SELECT id FROM subscriptions WHERE scope=? AND kind='alert')""",
                    (run_id, scope.key))
            widget_changed = status != run["status"] and (run["status"] == "starting" or run["status"] in ATTENTION
                                                          or status in ATTENTION or status in TERMINAL)
            updated = max(now, run["updated_at"]) if changed else run["updated_at"]
            db.execute("UPDATE runs SET status=?, updated_at=? WHERE run_id=?", (status, updated, run_id))
            run = dict(run)
            run.update(status=status, updated_at=updated, presentation=presentation or run["presentation"])
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
        if sub["kind"] == "alert" and self._foreground(db, sub):
            return  # this device is already displaying the destination
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
        if preview is None:
            preview = AlertPreview.from_json(run.get("presentation"))
        if activity:
            payload = activity_payload(run, scope, now, preview=preview, previews=self._device_previews(db, sub))
        else:
            payload = alert_payload(sub, run, scope, preview=preview)
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
            # Presence can arrive after enqueue or between retries. Drop only
            # unclaimed alerts; an APNs send already in flight cannot be recalled.
            db.execute("""DELETE FROM outbox WHERE state='pending' AND lease_until<=?
                AND subscription_id IN (SELECT s.id FROM subscriptions s JOIN mobile_presence p
                  ON p.principal=s.principal AND p.installation_id=s.installation_id
                  AND p.connection_id=s.connection_id AND p.scope=s.scope
                  WHERE s.kind='alert' AND p.expires_at>?)""", (now, now))
            row = db.execute("""SELECT o.*, s.token, s.environment, s.kind, s.preview_enabled, s.version AS token_version,
                s.principal, s.installation_id, s.connection_id, s.scope, s.profile
                FROM outbox o JOIN subscriptions s ON o.subscription_id=s.id
                WHERE o.state='pending' AND o.next_attempt<=? AND o.lease_until<=?
                AND o.expires_at>? AND s.expires_at>?
                AND (s.kind!='widget' OR s.token!='')
                AND (s.kind!='activity' OR s.last_timestamp<?)
                AND (s.kind IN ('activity','activity_start')
                     OR EXISTS (SELECT 1 FROM json_each(s.categories) WHERE value=o.category))
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
            if job["kind"] in {"activity", "activity_start"} and not self._device_previews(db, job):
                redact = redact_start if job["kind"] == "activity_start" else (
                    lambda aps, profile: redact_content_state(aps["content-state"], profile))
                redact(job["payload"]["aps"], job["profile"])
            if job["kind"] == "activity_start":
                job["payload"]["aps"]["timestamp"] = int(now)
                job["payload"]["aps"]["stale-date"] = int(now + 120)
            job["attempts"] += 1
            if job["kind"] == "activity":
                # Order published states rather than adding future seconds for token events.
                # A second transition this second waits until the next real clock second.
                aps = job["payload"]["aps"]
                if aps["content-state"]["status"] in ATTENTION:
                    # Alert only for this device's opted-in destination, evaluated
                    # at delivery so a queued update respects a later opt-out.
                    alert = db.execute("""SELECT a.* FROM subscriptions a JOIN subscriptions live
                        ON a.principal=live.principal AND a.installation_id=live.installation_id
                        AND a.connection_id=live.connection_id AND a.scope=live.scope
                        WHERE live.id=? AND a.kind='alert' AND a.expires_at>?
                        AND EXISTS (SELECT 1 FROM json_each(a.categories) WHERE value='attention')""",
                        (job["subscription_id"], now)).fetchone()
                    if alert is not None and not self._foreground(db, alert):
                        aps["alert"] = generic_alert(aps["content-state"]["status"])
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
                if job["kind"] == "activity_start":
                    # Apple rejected the push-to-start token itself: never use it again.
                    db.execute("""DELETE FROM activity_start_tokens WHERE principal=? AND installation_id=?
                        AND connection_id=? AND token=?""", (job["principal"], job["installation_id"],
                        job["connection_id"], job["token"]))
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
                run = db.execute(f"{_RUNS} WHERE run_id=?", (sub["run_id"],)).fetchone()
                if run is not None:
                    projected = {**dict(run), "status": "cancelled", "updated_at": max(now, run["updated_at"]),
                                 "activity_expired": True}
                    self._enqueue(db, sub, projected, Scope(sub["surface"], sub["profile"], sub["session_id"]), event_id)
        with self.transaction() as db:
            db.execute("DELETE FROM mobile_presence WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM widget_readers WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM alert_policies WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM activity_start_tokens WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM human_origins WHERE updated_at<?", (now - 30 * 86400,))
            db.execute("DELETE FROM subscriptions WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM outbox WHERE expires_at<=?", (now,))
            db.execute("DELETE FROM events WHERE created_at<?", (now - 30 * 86400,))
            db.execute("DELETE FROM runs WHERE updated_at<?", (now - 30 * 86400,))
            # Also the side rows of runs the predecessor pruned while it was rolled back to.
            db.execute("DELETE FROM run_state WHERE run_id NOT IN (SELECT run_id FROM runs)")

    def close(self):
        with self._lock:
            self._db.close()
