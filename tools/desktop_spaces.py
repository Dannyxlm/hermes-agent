"""Partial Desktop projection and request decisions without browser input.

Only a trusted backend may publish a sign-in request. There is deliberately no
production publisher or control adapter until Cua's ticket and input gates exist.
"""
from __future__ import annotations

import ipaddress
import json
import math
import re
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

from .desktop_space_lease import LeaseStore, digest, resource_id


UNAVAILABLE = ['producer', 'control', 'history']
BLOCKED_LOGIN_HOSTS = ('facebook.com', 'facebook.net', 'instagram.com', 'messenger.com',
                       'threads.net', 'threads.com', 'meta.com', 'fb.com', 'whatsapp.com')
RETENTION_SECONDS = 30 * 24 * 60 * 60
SCOPE_CAP = 1000


def login_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or not host or not host.isascii() or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or any(ch.isspace() for ch in origin)
            or '%' in origin or '\\' in origin or not '.' in host
            or all(re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', label, re.IGNORECASE) for label in host.split('.'))
            or host.endswith(('.localhost', '.local', '.internal'))
            or any(host == blocked or host.endswith('.' + blocked) for blocked in BLOCKED_LOGIN_HOSTS)):
        raise ValueError('Desktop sign-in origin denied')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('Desktop sign-in origin denied')
    if parsed.port not in (None, 443) or host != host.lower() or host.endswith('.'):
        raise ValueError('Desktop sign-in origin denied')
    return f'https://{host}'


def _revision(db, resource):
    row = db.execute('SELECT revision FROM revisions WHERE resource=?', (resource,)).fetchone() if db else None
    return row[0] if row else 1


def _receipt(action_id, status, revision, epoch, now):
    return {'action_id': action_id, 'status': status, 'revision': revision,
            'lease_epoch': epoch, 'server_time': now}


class SpaceStore:
    def __init__(self, path: Path):
        self.leases = LeaseStore(path)

    def snapshot(self, resource: str, scope: str, *, limit: int = 20, now: float | None = None) -> dict:
        resource_id(resource)
        now = time.time() if now is None else now
        with self.leases.connection() as db:
            lease = self.leases.lease_in(db, resource)
            signin, activity = None, []
            if db:
                row = db.execute('SELECT * FROM signins WHERE resource=? AND scope_hash=? AND resolved=0 AND expires_at>?',
                                 (resource, digest(scope), now)).fetchone()
                if row:
                    signin = {'id': row['request_id'], 'revision': row['revision'], 'expires_at': row['expires_at'],
                              'origin': row['origin'], 'reason': row['reason'], 'allowed_actions': ['not_now'],
                              'login_handle': None, 'masked_identifier': None}
                activity = [dict(row) for row in db.execute(
                    'SELECT id,label,at FROM activity WHERE resource=? AND scope_hash=? AND at>? ORDER BY at DESC,id DESC LIMIT ?',
                    (resource, digest(scope), now - RETENTION_SECONDS, limit))]
            return {'protocol_version': 1, 'resource_id': resource, 'revision': _revision(db, resource),
                    'server_time': now, 'state': 'signin' if signin else 'watch', 'complete': False,
                    'unavailable': list(UNAVAILABLE), 'lease': lease.public(scope, now), 'active': None,
                    'signin': signin, 'activity': activity, 'history': [], 'next_cursor': None}

    def publish_signin(self, resource: str, scope: str, *, request_id: str, revision: int,
                       origin: str, reason: str, expires_at: float) -> None:
        resource_id(resource)
        origin = login_origin(origin)
        if (not scope or not 0 < len(request_id) <= 128 or not 0 < len(reason) <= 512
                or revision < 1 or not math.isfinite(expires_at)):
            raise ValueError('Invalid desktop sign-in request')
        with self.leases.connection(write=True) as db:
            current = db.execute('SELECT * FROM signins WHERE resource=? AND scope_hash=?',
                                 (resource, digest(scope))).fetchone()
            if current and current['request_id'] == request_id and revision <= current['revision']:
                raise ValueError('Desktop sign-in revision must advance')
            db.execute('INSERT OR REPLACE INTO signins VALUES(?,?,?,?,?,?,?,0)',
                       (resource, digest(scope), request_id, revision, origin, reason, expires_at))
            db.execute('INSERT INTO revisions VALUES(?,2) ON CONFLICT(resource) DO UPDATE SET revision=revision+1', (resource,))

    def action_status(self, resource: str, scope: str, action_id: str, *, now: float | None = None) -> dict:
        resource_id(resource)
        now = time.time() if now is None else now
        with self.leases.connection() as db:
            if db:
                row = db.execute("SELECT payload FROM receipts WHERE resource=? AND scope_hash=? AND action_id=? AND json_extract(payload, '$.server_time')>?",
                                 (resource, digest(scope), action_id, now - RETENTION_SECONDS)).fetchone()
                if row:
                    return json.loads(row['payload'])
            return _receipt(action_id, 'unknown', _revision(db, resource), self.leases.lease_in(db, resource).epoch, now)

    def not_now(self, resource: str, scope: str, body: dict, *, now: float | None = None) -> dict:
        resource_id(resource)
        now = time.time() if now is None else now
        intent = {key: value for key, value in body.items()
                  if key not in ('profile', 'space', 'connection_id', 'installation_id')}
        fingerprint = digest(json.dumps(intent, sort_keys=True, separators=(',', ':'), allow_nan=False))
        with self.leases.connection(write=True) as db:
            self._prune(db, resource, scope, now)
            revision = _revision(db, resource)
            lease = self.leases.lease_in(db, resource)
            old = db.execute('SELECT * FROM receipts WHERE resource=? AND scope_hash=? AND action_id=?',
                             (resource, digest(scope), body['action_id'])).fetchone()
            if old:
                return json.loads(old['payload']) if old['fingerprint'] == fingerprint else _receipt(
                    body['action_id'], 'conflict', revision, lease.epoch, now)
            status = self._decide_not_now(db, resource, scope, body, revision, lease.epoch, now)
            if status == 'accepted':
                db.execute('UPDATE revisions SET revision=revision+1 WHERE resource=?', (resource,))
                revision += 1
                db.execute('INSERT INTO activity VALUES(?,?,?,?,?)',
                           (uuid.uuid4().hex, resource, digest(scope), 'Sign-in deferred', now))
            receipt = _receipt(body['action_id'], status, revision, lease.epoch, now)
            db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',
                       (resource, digest(scope), body['action_id'], fingerprint, json.dumps(receipt)))
            self._prune(db, resource, scope, now)
            return receipt

    @staticmethod
    def _prune(db, resource, scope, now):
        cutoff = now - RETENTION_SECONDS
        db.execute("DELETE FROM receipts WHERE json_extract(payload, '$.server_time')<=?", (cutoff,))
        db.execute('DELETE FROM activity WHERE at<=?', (cutoff,))
        db.execute("""DELETE FROM receipts WHERE rowid IN (
            SELECT rowid FROM receipts WHERE resource=? AND scope_hash=?
            ORDER BY json_extract(payload, '$.server_time') DESC, rowid DESC LIMIT -1 OFFSET ?)""",
            (resource, digest(scope), SCOPE_CAP))
        db.execute("""DELETE FROM activity WHERE rowid IN (
            SELECT rowid FROM activity WHERE resource=? AND scope_hash=?
            ORDER BY at DESC, rowid DESC LIMIT -1 OFFSET ?)""",
            (resource, digest(scope), SCOPE_CAP))

    @staticmethod
    def _decide_not_now(db, resource, scope, body, revision, epoch, now):
        if (body['action'] != 'not_now' or body['expected_revision'] != revision
                or body['expected_lease_epoch'] != epoch):
            return 'conflict'
        row = db.execute('SELECT * FROM signins WHERE resource=? AND scope_hash=?', (resource, digest(scope))).fetchone()
        if (not row or row['resolved'] or row['request_id'] != body['request_id']
                or row['revision'] != body['request_revision']):
            return 'conflict'
        if now >= row['expires_at']:
            return 'expired'
        db.execute('UPDATE signins SET resolved=1 WHERE resource=? AND scope_hash=?', (resource, digest(scope)))
        return 'accepted'
