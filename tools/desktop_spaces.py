"""Owner/profile Desktop projections and durable sign-in requests.

The credentialed door, not these records, owns the physical input fence.
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


def owner_scope(provider: str, user_id: str, home: Path, resource: str) -> str:
    resource_id(resource)
    return digest(json.dumps([provider, user_id, str(home.resolve()), resource], separators=(',', ':')))


def producer_context():
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.profiles import _get_default_hermes_home
    from hermes_cli.config import load_config_readonly

    home, root = get_hermes_home(), _get_default_hermes_home()
    token = set_hermes_home_override(root)
    try:
        policy = load_config_readonly().get('cloudseed_mobile', {})
    finally:
        reset_hermes_home_override(token)
    provider, user_id = policy.get('owner_provider'), policy.get('owner_user_id')
    if not isinstance(provider, str) or not provider or not isinstance(user_id, str) or not user_id:
        raise ValueError('Desktop owner policy unavailable')
    return SpaceStore(root / 'desktop-spaces.sqlite'), owner_scope(provider, user_id, home, 'desktop')


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
        unavailable = list(UNAVAILABLE)
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
                    binding = self.signin_binding(db, resource, scope)
                    if binding and binding['task_id'] and binding['target_id']:
                        unavailable.remove('producer')
                        signin['allowed_actions'] = ['take_over', 'not_now']
                    if binding and binding['request_id'] == row['request_id'] and binding['login_handle']:
                        signin.update(allowed_actions=['saved_login', 'take_over', 'not_now'],
                                      login_handle=binding['login_handle'], masked_identifier=binding['masked_identifier'])
                activity = [dict(row) for row in db.execute(
                    'SELECT id,label,at FROM activity WHERE resource=? AND scope_hash=? AND at>? ORDER BY at DESC,id DESC LIMIT ?',
                    (resource, digest(scope), now - RETENTION_SECONDS, limit))]
            return {'protocol_version': 1, 'resource_id': resource, 'revision': _revision(db, resource),
                    'server_time': now, 'state': 'signin' if signin else 'watch', 'complete': False,
                    'unavailable': unavailable, 'lease': lease.public(scope, now), 'active': None,
                    'signin': signin, 'activity': activity, 'history': [], 'next_cursor': None}

    def publish_signin(self, resource: str, scope: str, *, request_id: str, revision: int,
                       origin: str, reason: str, expires_at: float, login_handle: str | None = None,
                       masked_identifier: str | None = None, task_id: str = '', target_id: str = '') -> None:
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
            db.execute('INSERT OR REPLACE INTO signin_bindings VALUES(?,?,?,?,?,?,?,?)',
                       (resource, digest(scope), request_id, login_handle, masked_identifier, task_id, target_id, 'pending'))
            db.execute('INSERT INTO revisions VALUES(?,2) ON CONFLICT(resource) DO UPDATE SET revision=revision+1', (resource,))

    @staticmethod
    def signin_binding(db, resource, scope):
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signin_bindings'").fetchone()
        return db.execute('SELECT * FROM signin_bindings WHERE resource=? AND scope_hash=?',
                          (resource, digest(scope))).fetchone() if exists else None

    def signin_outcome(self, resource, scope, request_id, *, now=None):
        now = time.time() if now is None else now
        with self.leases.connection() as db:
            row = db.execute('SELECT * FROM signins WHERE resource=? AND scope_hash=?',
                             (resource, digest(scope))).fetchone() if db else None
            if not row or row['request_id'] != request_id:
                return 'expired'
            binding = self.signin_binding(db, resource, scope)
            outcome = binding['outcome'] if binding else 'pending'
            if outcome in ('filled', 'deferred', 'human_done'):
                return outcome
            return 'expired' if now >= row['expires_at'] else 'pending'

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
        db.execute('''DELETE FROM desktop_operations WHERE NOT EXISTS (
            SELECT 1 FROM receipts WHERE receipts.resource=desktop_operations.resource
            AND receipts.scope_hash=desktop_operations.scope_hash
            AND receipts.action_id=desktop_operations.action_id)''')

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
        binding = SpaceStore.signin_binding(db, resource, scope)
        if binding and binding['outcome'] != 'pending':
            return 'conflict'
        db.execute('UPDATE signins SET resolved=1 WHERE resource=? AND scope_hash=?', (resource, digest(scope)))
        db.execute("UPDATE signin_bindings SET outcome='deferred' WHERE resource=? AND scope_hash=?", (resource, digest(scope)))
        return 'accepted'
