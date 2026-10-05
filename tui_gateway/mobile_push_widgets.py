"""Read-only widget capabilities over the existing per-device push projection."""

import hashlib
import re


def reader_digest(token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invalid widget credential")
    return hashlib.sha256(token.encode()).hexdigest()


class WidgetPushStore:
    """Shares PushStore's SQLite transaction/lock; owns no worker or connection."""

    def _save_widget_reader(self, db, principal, installation_id, connection_id, token, expires):
        digest = reader_digest(token)
        existing = db.execute("SELECT * FROM widget_readers WHERE token_hash=?", (digest,)).fetchone()
        identity = (principal, installation_id, connection_id)
        if existing and tuple(existing[k] for k in ("principal", "installation_id", "connection_id")) != identity:
            raise ValueError("widget credential already assigned")
        db.execute("""INSERT INTO widget_readers VALUES (?,?,?,?,?)
            ON CONFLICT(principal, installation_id, connection_id) DO UPDATE
            SET token_hash=excluded.token_hash, expires_at=excluded.expires_at""",
            (digest, *identity, expires))

    def widget_reader(self, token):
        try:
            digest = reader_digest(token)
        except ValueError:
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM widget_readers WHERE token_hash=? AND expires_at>?",
                                   (digest, self.clock())).fetchone()
            return dict(row) if row else None

    def register_widget_inbox(self, principal, *, installation_id, connection_id, profile, home, token):
        from pathlib import Path
        import uuid
        from .mobile_push_store import normalized_uuid
        installation_id, connection_id = normalized_uuid(installation_id), normalized_uuid(connection_id)
        identity = (principal, installation_id, connection_id)
        expires = self.clock() + 30 * 86400
        home = str(Path(home).resolve())
        with self.transaction() as db:
            previous = self.widget_reader(token)
            self._save_widget_reader(db, *identity, token, expires)
            old = db.execute("SELECT * FROM widget_inbox_grants WHERE principal=? AND installation_id=? AND connection_id=?", identity).fetchone()
            grant_id = old['grant_id'] if previous and old and (old['profile'], old['home']) == (profile, home) else str(uuid.uuid4())
            db.execute("""INSERT INTO widget_inbox_grants VALUES (?,?,?,?,?,?)
                ON CONFLICT(principal, installation_id, connection_id) DO UPDATE
                SET profile=excluded.profile, home=excluded.home, grant_id=excluded.grant_id""",
                (*identity, profile, home, grant_id))
        return {"expires_at": expires, "profile": profile, "grant_id": grant_id,
                "snapshot_path": "/api/mobile/widgets/snapshot", "protocol_version": 2}

    def widget_inbox_grant(self, token):
        with self._lock:
            reader = self.widget_reader(token)
            if reader is None:
                raise PermissionError("widget credential expired")
            row = self._db.execute("SELECT * FROM widget_inbox_grants WHERE principal=? AND installation_id=? AND connection_id=?",
                tuple(reader[k] for k in ('principal', 'installation_id', 'connection_id'))).fetchone()
            if row is None:
                raise PermissionError("Inbox grant required")
            return {**reader, **dict(row)}

    def widget_snapshot(self, token):
        # One transaction binds credential validation and data selection to logout.
        with self.transaction() as db:
            reader = self.widget_reader(token)
            if reader is None:
                raise PermissionError("widget credential expired")
            rows = db.execute("""SELECT r.* FROM runs r JOIN subscriptions s ON r.scope=s.scope
                WHERE s.principal=? AND s.installation_id=? AND s.connection_id=?
                AND s.kind='widget' AND s.surface IN ('chats', 'native_session') AND s.expires_at>?
                AND NOT EXISTS (SELECT 1 FROM runs newer WHERE newer.scope=r.scope
                  AND (newer.started_at>r.started_at OR (newer.started_at=r.started_at AND newer.rowid>r.rowid)))
                ORDER BY r.updated_at DESC LIMIT 1000""",
                (reader['principal'], reader['installation_id'], reader['connection_id'], self.clock())).fetchall()
            return {"protocol_version": 1, "installation_id": reader['installation_id'],
                    "connection_id": reader['connection_id'], "generated_at": self.clock(),
                    "items": [{"profile": row['profile'], "session_id": row['session_id'],
                               **{key: row[key] for key in ('run_id', 'status', 'started_at', 'updated_at')}}
                              for row in rows]}

    def update_widget_token(self, credential, token):
        if token != "" and (not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32,512}", token) or len(token) % 2):
            raise ValueError("invalid widget push token")
        with self.transaction() as db:
            reader = self.widget_reader(credential)
            if reader is None:
                raise PermissionError("widget credential expired")
            count = db.execute("""UPDATE subscriptions SET token=?, version=version+1
                WHERE principal=? AND installation_id=? AND connection_id=? AND kind='widget'
                AND expires_at>? AND token!=?""", (token.lower(), reader['principal'],
                reader['installation_id'], reader['connection_id'], self.clock(), token.lower())).rowcount
            return {"updated": count}

    def _enqueue_widget(self, db, sub, run, event_id):
        if not sub['token']:
            return
        now = self.clock()
        # Coalesce only unclaimed jobs for the same account/device. Keep the first
        # due date so a busy stream cannot postpone the reload indefinitely.
        siblings = """SELECT id FROM subscriptions WHERE principal=? AND installation_id=?
            AND connection_id=? AND kind='widget' AND environment=?"""
        args = (sub['principal'], sub['installation_id'], sub['connection_id'], sub['environment'])
        prior = db.execute(f"SELECT MIN(next_attempt) FROM outbox WHERE subscription_id IN ({siblings}) AND state='pending' AND lease_until<=?",
                           (*args, now)).fetchone()[0]
        db.execute(f"DELETE FROM outbox WHERE subscription_id IN ({siblings}) AND state='pending' AND lease_until<=?", (*args, now))
        collapse = hashlib.sha256(':'.join(args).encode()).hexdigest()
        import json
        import uuid
        db.execute("""INSERT OR IGNORE INTO outbox
            (job_id, subscription_id, event_id, run_id, payload, urgent, collapse_id, expires_at, next_attempt, category)
            VALUES (?,?,?,?,?,0,?,?,?,'completion')""", (str(uuid.uuid4()), sub['id'], event_id, run['run_id'],
            json.dumps({"aps": {"content-changed": True}}), collapse,
            min(sub['expires_at'], now + 3600), prior if prior is not None else now + 2))
