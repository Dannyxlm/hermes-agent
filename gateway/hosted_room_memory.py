"""Server-only trigger receipts and execution-time room memory attestation."""
from contextlib import contextmanager
from contextvars import ContextVar
import json

from gateway.owner_identity import same_owner
from persistence.gateway.hosted_rooms import room_memory_trigger_key

_trigger: ContextVar[dict | None] = ContextVar('hosted_room_trigger', default=None)


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
                         (room_memory_trigger_key(room_id, event_id), json.dumps({key: trigger[key] for key in ('subject', 'trigger')}, sort_keys=True)))
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
    from gateway.session_persistence import owner_rule

    trigger = service.store.hosted_rooms.memory_trigger(
        placement=service.placement, room_id=room_id, source_event_seq=task['payload']['source_event_seq'],
        authority_epoch=room['authority_epoch'], owner=owner_rule())
    if trigger is None:
        return None
    return {'version': 1, 'room_id': room_id, 'profiles': profiles,
            'member_profile': task['payload']['target_profile'], 'trigger': trigger}


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
