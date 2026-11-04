"""Server-only trigger receipts and execution-time room memory attestation."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json

from gateway.owner_identity import same_owner

_trigger: ContextVar[dict | None] = ContextVar('hosted_room_trigger', default=None)
_PREFIX = 'gateway.hosted.memory-trigger.v1:'


def _key(room_id, event_id):
    identity = json.dumps([room_id, event_id], separators=(',', ':'))
    return _PREFIX + hashlib.sha256(identity.encode()).hexdigest()


@contextmanager
def authenticated_room_trigger(subject, automation):
    """Only the authenticated canonical groups.send handler binds this scope."""
    active = [True]
    token = _trigger.set({'subject': subject, 'trigger': 'team-room' if automation else 'owner', 'active': active})
    try:
        yield
    finally:
        active[0] = False
        _trigger.reset(token)


def record_trigger(conn, room_id, event_id, payload):
    """Called inside append_event's transaction; never bless a historical/replayed event."""
    trigger = _trigger.get()
    if trigger is not None and trigger['active'][0] and conn.execute(
            'SELECT 1 FROM hosted_room_events WHERE room_id=? AND event_id=?',
            (room_id, event_id)).fetchone() is None:
        owner = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             ('gateway.hosted.owner.v1:' + room_id,)).fetchone()
        if owner is not None and same_owner(owner[0], trigger['subject']):
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                         (_key(room_id, event_id), json.dumps({key: trigger[key] for key in ('subject', 'trigger')}, sort_keys=True)))
    return payload


def local_memory_context(service, task):
    """Describe only an owned, entirely local roster with an authenticated source event.

    The provider additionally selects its internal profile allowlist. No text from
    the room, task prompt or client payload participates in this decision.
    """
    room_id = task['identity'].room_id
    room = service._room(room_id)
    homes = service.profile_homes()
    profiles = []
    for member in room['members']:
        profile = member['profile']
        if ((member.get('target') or {}).get('kind', 'local') != 'local' or profile not in homes):
            return None
        profiles.append(profile)
    if not profiles:
        return None
    with service.authority.db._read_ctx() as conn:
        event = conn.execute('SELECT event_id,kind,actor_json,authority_epoch FROM hosted_room_events '
                             'WHERE room_id=? AND seq=?',
                             (room_id, task['payload']['source_event_seq'])).fetchone()
        if event is None or event['kind'] != 'message.user' or event['authority_epoch'] != room['authority_epoch']:
            return None
        receipt = conn.execute('SELECT value FROM state_meta WHERE key=?',
                               (_key(room_id, event['event_id']),)).fetchone()
        if receipt is None:
            return None
        trigger = json.loads(receipt[0])
    actor = json.loads(event['actor_json'])
    if (not same_owner(service._owner(room_id), trigger['subject'])
            or trigger['trigger'] not in {'owner', 'team-room'}
            or actor.get('kind') != 'user'
            or actor.get('id') != ('team-room' if trigger['trigger'] == 'team-room' else 'desktop')):
        return None
    return {'version': 1, 'room_id': room_id, 'profiles': profiles,
            'member_profile': task['payload']['target_profile'], 'trigger': trigger['trigger']}


def admission_memory_context(authority, ref, row):
    """Re-attest this exact admission, including after restart or a local profile hop."""
    if not str(row.get('request_id', '')).startswith('hosted:'):
        return None
    from gateway.session_hosted_transport import _check_remote_hosted_admission
    from gateway.session_authorities import owner_scope
    with owner_scope(authority):
        remote = _check_remote_hosted_admission(authority, ref, row, memory_context=True)
        if remote is not False:
            return remote
        service = getattr(authority, 'hosted_room_service', None)
        if service is None:
            return None
        return local_memory_context(service, service.check_admission(ref, row))
