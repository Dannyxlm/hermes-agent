"""Device-confirmed Apple Reminders commands; EventKit owns the actual records."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class ReminderError(ValueError):
    pass


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReminderError("Invalid identifier") from exc


def validate_request(operation, payload):
    if not isinstance(payload, dict):
        raise ReminderError("Object required")
    allowed = {"create": {"title", "notes", "due_at", "list_id"},
               "list": {"cursor", "include_completed"}, "complete": {"reminder_id"}}
    if operation not in allowed or set(payload) - allowed[operation]:
        raise ReminderError("Unsupported reminder request")
    for key, value in payload.items():
        if key == "include_completed":
            if not isinstance(value, bool):
                raise ReminderError("Invalid completion filter")
        elif not isinstance(value, str) or len(value) > (8000 if key == "notes" else 1000):
            raise ReminderError("Invalid reminder field")
    if operation == "create" and not payload.get("title", "").strip():
        raise ReminderError("Title required")
    if operation == "complete" and not payload.get("reminder_id"):
        raise ReminderError("Reminder identifier required")
    if payload.get("due_at"):
        from datetime import datetime
        try:
            parsed = datetime.fromisoformat(payload["due_at"].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
        except ValueError as exc:
            raise ReminderError("Due date requires ISO8601 with timezone") from exc
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class ReminderOutbox:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, profile TEXT NOT NULL,
                    secret_hash TEXT NOT NULL, enabled INTEGER NOT NULL, seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, device TEXT NOT NULL, operation TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    expires REAL NOT NULL, result TEXT);
                CREATE INDEX IF NOT EXISTS pending_device ON commands(device, state, created);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _device(self, db, device, principal=None, profile=None, secret=None):
        row = db.execute("SELECT * FROM devices WHERE id=?", (identifier(device),)).fetchone()
        if (not row or not row["enabled"] or
                (principal is not None and row["principal"] != principal) or
                (profile is not None and row["profile"] != profile) or
                (secret is not None and not hmac.compare_digest(row["secret_hash"], self._digest(secret)))):
            raise ReminderError("Device is not connected")
        return row

    @staticmethod
    def _digest(secret):
        if not isinstance(secret, str) or not re.fullmatch(r"[a-f0-9]{64}", secret):
            raise ReminderError("Invalid device credential")
        return hashlib.sha256(secret.encode()).hexdigest()

    def register(self, principal, profile, device, secret):
        device, digest = identifier(device), self._digest(secret)
        with self.connection() as db:
            old = db.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
            if old:
                self._device(db, device, principal, profile, secret)
                db.execute("UPDATE devices SET seen=? WHERE id=?", (self.clock(), device))
            else:
                db.execute("INSERT INTO devices VALUES (?,?,?,?,1,?)", (device, principal, profile, digest, self.clock()))
        return {"ok": True, "protocol_version": 1}

    def rebind(self, principal, profile, device, secret):
        """Retain stable requests without redelivery or reviving disabled devices."""
        with self.connection() as db:
            self._device(db, device, profile=profile, secret=secret)
            db.execute("UPDATE devices SET principal=? WHERE id=?", (principal, device))
        return {"ok": True, "protocol_version": 1}

    def devices(self, profile):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,profile,seen FROM devices WHERE profile=? AND enabled=1 ORDER BY seen DESC", (profile,))]

    def enqueue(self, device, request_id, operation, payload, profile):
        device, request_id = identifier(device), identifier(request_id)
        payload = validate_request(operation, payload)
        with self.connection() as db:
            self._device(db, device, profile=profile)
            old = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if old:
                if (old["device"], old["operation"], old["payload"]) != (device, operation, payload):
                    raise ReminderError("Request identifier already used")
            else:
                count = db.execute("SELECT COUNT(*) FROM commands WHERE device=? AND state IN ('queued','processing') AND expires>?",
                                   (device, self.clock())).fetchone()[0]
                if count >= 100:
                    raise ReminderError("Device queue is full")
                db.execute("INSERT INTO commands VALUES (?,?,?,?,'queued',?,?,NULL)",
                           (request_id, device, operation, payload, self.clock(), self.clock() + 86400))
        return self.status(device, request_id, profile)

    def status(self, device, request_id, profile):
        with self.connection() as db:
            self._device(db, device, profile=profile)
            row = db.execute("SELECT * FROM commands WHERE id=? AND device=?", (identifier(request_id), identifier(device))).fetchone()
            if not row:
                raise ReminderError("Request not found")
            state = row["state"]
            if state in ("queued", "processing") and row["expires"] <= self.clock():
                state = "expired" if state == "queued" else "unconfirmed"
            return {"id": row["id"], "state": state, "result": json.loads(row["result"]) if row["result"] else None}

    def poll(self, principal, profile, device, secret):
        with self.connection() as db:
            self._device(db, device, principal, profile, secret)
            db.execute("UPDATE devices SET seen=? WHERE id=?", (self.clock(), identifier(device)))
            row = db.execute("SELECT * FROM commands WHERE device=? AND state IN ('queued','processing') AND expires>? ORDER BY created,id LIMIT 1",
                             (identifier(device), self.clock())).fetchone()
            if not row:
                return {"ok": True, "protocol_version": 1, "command": None}
            db.execute("UPDATE commands SET state='processing' WHERE id=?", (row["id"],))
            return {"ok": True, "protocol_version": 1, "command": {
                "id": row["id"], "operation": row["operation"], "payload": json.loads(row["payload"]), "expires_at": row["expires"]}}

    def acknowledge(self, principal, profile, device, secret, request_id, state, result):
        if state not in ("completed", "failed", "unconfirmed") or not isinstance(result, dict):
            raise ReminderError("Invalid result")
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 256000:
            raise ReminderError("Result is too large")
        with self.connection() as db:
            self._device(db, device, principal, profile, secret)
            row = db.execute("SELECT * FROM commands WHERE id=? AND device=?", (identifier(request_id), identifier(device))).fetchone()
            if not row or row["state"] == "queued":
                raise ReminderError("Request was not delivered")
            if row["state"] != "processing":
                if row["state"] != state or row["result"] != encoded:
                    raise ReminderError("Result already recorded")
            else:
                db.execute("UPDATE commands SET state=?,result=? WHERE id=?", (state, encoded, row["id"]))
        return {"ok": True, "protocol_version": 1}

    def disconnect(self, principal, profile, device, secret):
        with self.connection() as db:
            self._device(db, device, principal, profile, secret)
            db.execute("UPDATE devices SET enabled=0 WHERE id=?", (identifier(device),))
            db.execute("UPDATE commands SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'unconfirmed' END,payload='{}' WHERE device=? AND state IN ('queued','processing')", (identifier(device),))
        return {"ok": True, "protocol_version": 1}

