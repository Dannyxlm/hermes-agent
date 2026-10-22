"""Durable decisions coordinated with the door's authoritative input fence."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from tools.desktop_door_client import DoorClient, DoorConflict, DoorUnavailable
from tools.desktop_space_lease import SpaceLease, digest
from tools.desktop_spaces import _receipt, _revision


def _fingerprint(body):
    intent = {key: value for key, value in body.items()
              if key not in ('profile', 'space', 'connection_id', 'installation_id')}
    return digest(json.dumps(intent, sort_keys=True, separators=(',', ':'), allow_nan=False))


class DesktopController:
    def __init__(self, store, scope: str, home: Path):
        self.store, self.scope = store, scope
        self.profile = digest(str(home.resolve()))
        self.door = DoorClient.configured()

    def _session(self, state, viewer):
        matches = [session for session in state.sessions if session.viewer_id == viewer
                   and session.owner_id == self.scope and session.profile_id == self.profile]
        if len(matches) != 1 or matches[0].expires_at <= time.time():
            raise DoorConflict('Desktop viewer changed')
        return matches[0]

    @staticmethod
    def _human_session(state):
        sessions = [session for session in state.sessions if session.viewer_id == state.human_viewer_id
                    and session.mode == 'control' and session.lease_epoch == state.epoch
                    and session.expires_at == state.expires_at]
        if (not state.ready or state.holder != 'human' or state.state != 'HUMAN'
                or not state.gate.drained() or len(sessions) != 1 or state.expires_at is None
                or not time.time() < state.expires_at <= time.time() + 121):
            raise DoorUnavailable('Desktop human state unconfirmed')
        return sessions[0]

    def sync(self, state):
        if state.agent_open():
            next_lease = SpaceLease('agent', state.epoch)
        else:
            session = self._human_session(state)
            next_lease = SpaceLease('human', state.epoch, digest(session.owner_id),
                                    digest(session.viewer_id), state.expires_at)
        previous = self.store.leases.read('desktop')
        if state.epoch < previous.epoch:
            raise DoorUnavailable('Desktop epoch regressed')
        if previous == next_lease or (previous.holder == 'human_pending' and previous.epoch == state.epoch):
            return
        if previous.holder == 'human' and next_lease.holder == 'agent' and next_lease.epoch <= previous.epoch:
            raise DoorUnavailable('Desktop hand-back unconfirmed')
        with self.store.leases.connection(write=True) as db:
            if self.store.leases.lease_in(db, 'desktop') != previous:
                raise DoorConflict('Desktop lease changed')
            self.store.leases._save(db, 'desktop', next_lease)
            if previous.holder == 'human' and next_lease.holder == 'agent':
                db.execute("UPDATE signin_bindings SET outcome='human_done' WHERE resource='desktop' AND scope_hash=? AND outcome='pending'",
                           (previous.scope_hash,))
                db.execute("UPDATE signins SET resolved=1 WHERE resource='desktop' AND scope_hash=? AND request_id IN (SELECT request_id FROM signin_bindings WHERE resource='desktop' AND scope_hash=? AND outcome='human_done')",
                           (previous.scope_hash, previous.scope_hash))

    def snapshot(self, *, limit):
        state = self.door.state()
        self.sync(state)
        result = self.store.snapshot('desktop', self.scope, limit=limit)
        if state.ready and (state.agent_open() or state.state == 'HUMAN'):
            result['unavailable'].remove('control')
        if result['signin'] and result['signin']['login_handle']:
            result['unavailable'].remove('producer')
        if state.state == 'HUMAN':
            result['state'] = 'takeover'
        return result

    def observe(self, body):
        if body['mode'] == 'replay':
            raise DoorUnavailable('Replay unavailable')
        state = self.door.state()
        self.sync(state)
        if body['expected_lease_epoch'] != state.epoch:
            raise DoorConflict('Desktop epoch changed')
        if body['mode'] == 'watch':
            observation = self.door.observe(self.scope, self.profile)
        else:
            if not state.ready:
                raise DoorUnavailable('Desktop control unavailable')
            session = self._session(state, body['viewer_id'])
            if (state.human_viewer_id != session.viewer_id or session.mode != 'control'
                    or not self.store.leases.permits('desktop', self.scope, session.viewer_id, state.epoch)):
                raise DoorConflict('Desktop viewer changed')
            observation = self.door.renew(self.scope, self.profile, session.viewer_id, state.epoch)
        if (observation.mode != body['mode'] or observation.lease_epoch != state.epoch
                or not time.time() < observation.expires_at <= time.time() + (601 if body['mode'] == 'watch' else 121)
                or (body['mode'] == 'control' and observation.viewer_id != body['viewer_id'])):
            raise DoorUnavailable('Desktop observation unconfirmed')
        if body['mode'] == 'control':
            renewed = self.door.state()
            self.sync(renewed)
            if renewed.epoch != observation.lease_epoch or renewed.expires_at != observation.expires_at:
                raise DoorUnavailable('Desktop renewal unconfirmed')
        return observation.model_dump()

    def _old_receipt(self, db, body):
        row = db.execute('SELECT * FROM receipts WHERE resource=? AND scope_hash=? AND action_id=?',
                         ('desktop', digest(self.scope), body['action_id'])).fetchone()
        if row:
            return json.loads(row['payload']) if row['fingerprint'] == _fingerprint(body) else _receipt(
                body['action_id'], 'conflict', _revision(db, 'desktop'), self.store.leases.lease_in(db, 'desktop').epoch, time.time())
        return None

    def _save_receipt(self, db, body, status):
        lease = self.store.leases.lease_in(db, 'desktop')
        receipt = _receipt(body['action_id'], status, _revision(db, 'desktop'), lease.epoch, time.time())
        db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',
                   ('desktop', digest(self.scope), body['action_id'], _fingerprint(body), json.dumps(receipt)))
        self.store._prune(db, 'desktop', self.scope, time.time())
        return receipt

    def _finish(self, body, status):
        with self.store.leases.connection(write=True) as db:
            if status == 'accepted':
                db.execute('INSERT INTO activity VALUES(?,?,?,?,?)',
                           (uuid.uuid4().hex, 'desktop', digest(self.scope),
                            {'saved_login': 'Saved login filled', 'take_over': 'Human control started',
                             'hand_back': 'Control handed back'}[body['action']], time.time()))
            return self._save_receipt(db, body, status)

    def action(self, body):
        with self.store.leases.connection() as db:
            old = self._old_receipt(db, body) if db else None
        if old:
            return self.action_status(body['action_id']) if old['status'] == 'outcome_unknown' else old
        state = self.door.state()
        self.sync(state)
        if not state.ready:
            raise DoorUnavailable('Desktop control unavailable')
        if body['action'] == 'saved_login':
            return self.saved_login(body, state)
        return self.transition(body, state)

    def _claim(self, db, body):
        old = self._old_receipt(db, body)
        if old:
            return old
        lease = self.store.leases.lease_in(db, 'desktop')
        if body['expected_revision'] != _revision(db, 'desktop') or body['expected_lease_epoch'] != lease.epoch:
            return self._save_receipt(db, body, 'conflict')
        return None

    def transition(self, body, state):
        viewer = self._session(state, body['viewer_id'])
        takeover = body['action'] == 'take_over'
        if takeover and (not state.agent_open() or viewer.mode != 'watch'):
            raise DoorUnavailable('Desktop gate unconfirmed')
        if not takeover and (viewer.mode != 'control' or not self.store.leases.permits(
                'desktop', self.scope, viewer.viewer_id, state.epoch)):
            raise DoorConflict('Desktop viewer changed')
        with self.store.leases.connection(write=True) as db:
            old = self._claim(db, body)
            if old:
                return old
            previous = self.store.leases.lease_in(db, 'desktop')
            if takeover:
                if previous.holder not in ('agent', 'none'):
                    return self._save_receipt(db, body, 'denied')
                self.store.leases._save(db, 'desktop', SpaceLease('human_pending', previous.epoch))
            self._save_receipt(db, body, 'outcome_unknown')
            db.execute('INSERT INTO desktop_operations VALUES(?,?,?,?,?,?)',
                       ('desktop', digest(self.scope), body['action_id'], body['action'], viewer.viewer_id, state.epoch))
        try:
            ack = self.door.transition('takeover' if takeover else 'handback', self.scope, self.profile,
                                       viewer.viewer_id, state.epoch, body['action_id'])
            if (ack.epoch != state.epoch + 1 or ack.holder != ('human' if takeover else 'agent')
                    or (takeover and (ack.viewer_id != viewer.viewer_id or not ack.gate.drained()))
                    or (not takeover and (ack.viewer_id is not None or ack.expires_at is not None or ack.gate.state != 'open'))):
                raise DoorUnavailable('Desktop transition unconfirmed')
        except (DoorUnavailable, DoorConflict):
            pass
        result = self.action_status(body['action_id'])
        if result['status'] == 'outcome_unknown' and takeover:
            with self.store.leases.connection(write=True) as db:
                current = self.store.leases.lease_in(db, 'desktop')
                if current.holder == 'human_pending' and current.epoch == previous.epoch:
                    self.store.leases._save(db, 'desktop', previous)
        return result

    def action_status(self, action_id):
        receipt = self.store.action_status('desktop', self.scope, action_id)
        if receipt['status'] != 'outcome_unknown':
            return receipt
        with self.store.leases.connection() as db:
            operation = db.execute('SELECT * FROM desktop_operations WHERE resource=? AND scope_hash=? AND action_id=?',
                                   ('desktop', digest(self.scope), action_id)).fetchone()
            operation = dict(operation) if operation else None
        if not operation:
            return receipt
        try:
            state = self.door.state()
            if state.epoch != operation['lease_epoch'] + 1:
                return receipt
            if operation['action'] == 'take_over':
                session = self._human_session(state)
                if session.viewer_id != operation['viewer_id'] or session.owner_id != self.scope or session.profile_id != self.profile:
                    return receipt
            elif not state.agent_open():
                return receipt
            self.sync(state)
        except (DoorUnavailable, DoorConflict):
            return receipt
        body = {'action_id': action_id, 'action': operation['action']}
        with self.store.leases.connection(write=True) as db:
            current = self.store.action_status('desktop', self.scope, action_id)
            if current['status'] != 'outcome_unknown':
                return current
            current.update(status='accepted', revision=_revision(db, 'desktop'), lease_epoch=state.epoch)
            db.execute('UPDATE receipts SET payload=? WHERE resource=? AND scope_hash=? AND action_id=?',
                       (json.dumps(current), 'desktop', digest(self.scope), action_id))
            db.execute('INSERT INTO activity VALUES(?,?,?,?,?)', (uuid.uuid4().hex, 'desktop', digest(self.scope),
                       'Human control started' if body['action'] == 'take_over' else 'Control handed back', time.time()))
        return current

    def saved_login(self, body, state):
        if not state.agent_open():
            raise DoorUnavailable('Desktop agent gate unconfirmed')
        with self.store.leases.connection(write=True) as db:
            old = self._claim(db, body)
            if old:
                return old
            row = db.execute('SELECT * FROM signins WHERE resource=? AND scope_hash=?',
                             ('desktop', digest(self.scope))).fetchone()
            binding = self.store.signin_binding(db, 'desktop', self.scope)
            status = self._signin_decision(row, binding, body)
            if status != 'accepted':
                return self._save_receipt(db, body, status)
            lease = self.store.leases.lease_in(db, 'desktop')
            if lease.holder not in ('agent', 'none'):
                return self._save_receipt(db, body, 'denied')
            self._save_receipt(db, body, 'outcome_unknown')
            db.execute("UPDATE signin_bindings SET outcome='filling' WHERE resource=? AND scope_hash=?",
                       ('desktop', digest(self.scope)))
            binding, origin = dict(binding), row['origin']
        from tools.browser_vault_signin import fill_requested_login
        try:
            filled = fill_requested_login(binding, origin)
        except (OSError, RuntimeError, ValueError, TypeError):
            return self.store.action_status('desktop', self.scope, body['action_id'])
        with self.store.leases.connection(write=True) as db:
            db.execute('UPDATE signin_bindings SET outcome=? WHERE resource=? AND scope_hash=? AND request_id=?',
                       ('filled' if filled else 'deferred', 'desktop', digest(self.scope), body['request_id']))
            db.execute('UPDATE signins SET resolved=1 WHERE resource=? AND scope_hash=? AND request_id=?',
                       ('desktop', digest(self.scope), body['request_id']))
            db.execute('UPDATE revisions SET revision=revision+1 WHERE resource=?', ('desktop',))
        return self._finish(body, 'accepted' if filled else 'denied')

    @staticmethod
    def _signin_decision(row, binding, body):
        if (not row or row['resolved'] or row['request_id'] != body['request_id']
                or row['revision'] != body['request_revision'] or not binding
                or binding['request_id'] != row['request_id'] or binding['outcome'] != 'pending'
                or not binding['login_handle'] or binding['login_handle'] != body['login_handle']):
            return 'conflict'
        return 'expired' if time.time() >= row['expires_at'] else 'accepted'
