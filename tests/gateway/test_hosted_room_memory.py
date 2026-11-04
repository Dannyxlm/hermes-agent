"""Room memory authority comes from authenticated controls, not prompt/payload fields."""
import asyncio
import json
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.fixture
def room(tmp_path, monkeypatch):
    from gateway.session_hosted_service import CanonicalHostedRoomService
    from gateway.hosted_rooms import create_room, local_authority_gateway_id
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with SessionDB(tmp_path / 'state.db') as db:
        authority = SimpleNamespace(db=db, profile_id=str(tmp_path), epoch=begin_runtime_epoch(db, instance_id='test'))
        from gateway.session_authority import SessionAuthority
        authority.logical_owner = lambda session_id: SessionAuthority.logical_owner(authority, session_id)
        service = authority.hosted_room_service = CanonicalHostedRoomService(authority, None)
        monkeypatch.setattr(service, 'profile_homes', lambda: {'default': tmp_path, 'atlas': tmp_path / 'profiles/atlas'})
        monkeypatch.setattr(service.runtime, 'status', lambda: {'running': True, 'stopping': False})
        monkeypatch.setattr(service, '_prepare_tracked', lambda b: None)
        monkeypatch.setattr(service.runtime, 'wakeup', lambda: None)
        service.authorize_room('owner', 'room', create=True)
        gateway = local_authority_gateway_id()
        create_room(db.db_path, room_id='room', name='Room', authority_gateway_id=gateway, members=[
            {'member_id': 'default', 'profile': 'default', 'handle': 'ava'},
            {'member_id': 'atlas', 'profile': 'atlas', 'handle': 'atlas'}])
        yield authority, service, gateway


def submit_task(room, *, automation=False, legacy=False):
    from gateway.session_group_controls import _group
    from gateway.session_contract import Principal
    from gateway import hosted_room_driver as tasks
    from tui_gateway.hosted_room_driver import HostedRoomBinding
    authority, service, gateway = room
    if legacy:
        from gateway.hosted_rooms import user_event_id
        event = service.send(room_id='room', event_id=user_event_id('input'), payload={'text': 'input', 'thread_id': 'main'})
    else:
        event = _group(authority, Principal('owner', authority.profile_id, frozenset(), 'test'), 'groups.send',
                       {'room_id': 'room', 'event_id': 'input', 'payload': {'text': 'input', 'thread_id': 'main'}},
                       automation=automation)['event']
    identity = tasks.TaskIdentity('room', 'task', 'main', 'turn')
    payload = {'target_profile': 'default', 'target_member_id': 'default',
               'source_event_seq': event['seq'], 'prompt': 'frozen'}
    tasks.admit_task(authority.db.db_path, identity, payload=payload, clock=time.time)
    lease = tasks.acquire_lease(authority.db.db_path, room_id='room', gateway_id=gateway, authority_epoch=1,
                               process_generation='test', ttl_seconds=30, clock=time.time)
    tasks.start_task(authority.db.db_path, identity, lease, expected_cancel_generation=0, clock=time.time)
    task, = tasks.list_tasks(authority.db.db_path, room_id='room')
    rpc = service._resolve_member_transport(HostedRoomBinding('room', gateway, 1), task)
    row = {'admission_id': 'admission', 'principal_id': 'owner',
           'request_id': 'hosted:' + json.dumps([asdict(identity), task['execution_generation']]),
           'payload': {'text': 'frozen'}}
    return rpc.ref, row, task


@pytest.mark.parametrize('automation', [False, True])
def test_authenticated_trigger_reaches_only_its_executing_turn(room, monkeypatch, automation):
    from agent.hosted_room_memory import current_hosted_room_memory
    from gateway.session_finite import execute_finite_admission
    authority, _service, _ = room
    ref, row, _task = submit_task(room, automation=automation)
    async def execute(a, r, admission):
        context = current_hosted_room_memory(ref.session_id, authority.profile_id)
        assert context['profiles'] == ['default', 'atlas']
        assert context['trigger'] == ('team-room' if automation else 'owner')
        assert context['room_id'] == 'room'
        assert current_hosted_room_memory('other-session', authority.profile_id) is None
        assert current_hosted_room_memory(ref.session_id, '/foreign') is None
        return 'verified'
    monkeypatch.setattr('gateway.session_ingress.execute_admission', execute)
    assert asyncio.run(execute_finite_admission(authority, ref, row)) == 'verified'
    assert current_hosted_room_memory(ref.session_id, authority.profile_id) is None


@pytest.mark.parametrize('unsafe', ['legacy', 'peer', 'missing-local', 'foreign', 'spoof'])
def test_unsafe_room_has_no_memory_authority(room, monkeypatch, unsafe):
    from gateway.hosted_room_memory import local_memory_context
    authority, service, _ = room
    _ref, _row, task = submit_task(room, legacy=unsafe == 'legacy')
    if unsafe in {'peer', 'missing-local'}:
        members = service._room('room')['members']
        if unsafe == 'peer':
            members[1]['target'] = {'kind': 'peer', 'gateway_id': 'remote', 'profile': 'atlas'}
        else:
            monkeypatch.setattr(service, 'profile_homes', lambda: {'default': authority.profile_id})
        with authority.db._lock:
            authority.db._conn.execute("UPDATE hosted_rooms SET members_json=? WHERE room_id='room'", (json.dumps(members),))
            authority.db._conn.commit()
    if unsafe == 'foreign':
        with authority.db._lock:
            authority.db._conn.execute("UPDATE state_meta SET value='foreign' WHERE key='gateway.hosted.owner.v1:room'")
            authority.db._conn.commit()
    if unsafe == 'spoof':
        # Neither a client dictionary nor an unknown source coordinate can mint a receipt.
        task = {**task, 'payload': {**task['payload'], 'source_event_seq': 999, 'memory_context': {'trusted': True}}}
    assert local_memory_context(service, task) is None


def test_foreign_control_and_client_attestation_field_are_rejected(room):
    from gateway.session_controls import AuthorityConnection
    authority, _, _ = room
    authority.instance_id, authority.events = 'test', {}
    async def probe():
        for subject, extra, expected in [('foreign', {}, 'permission_denied'),
                                          ('owner', {'hosted_room_memory': {'trusted': True}}, 'invalid_params')]:
            connection = AuthorityConnection(authority, object(), {'user_id': subject})
            result = await connection.dispatch({'id': 1, 'method': 'groups.send', 'params': {
                'room_id': 'room', 'event_id': 'bad', 'payload': {'text': 'input', 'thread_id': 'main'}, **extra}})
            assert result['error']['message'] == expected
    asyncio.run(probe())


def test_trigger_receipt_is_atomic_and_never_backfills_legacy_events(room, monkeypatch):
    from gateway import hosted_rooms
    from gateway.hosted_room_memory import local_memory_context
    from gateway.session_contract import Principal
    from gateway.session_group_controls import _group
    authority, service, _ = room
    actor = Principal('owner', authority.profile_id, frozenset(), 'test')
    params = {'room_id': 'room', 'event_id': 'input', 'payload': {'text': 'input', 'thread_id': 'main'}}
    original = hosted_rooms._insert_event
    def fail(*args, **kwargs):
        raise RuntimeError('fixture storage failure')
    monkeypatch.setattr(hosted_rooms, '_insert_event', fail)
    with pytest.raises(RuntimeError, match='fixture storage failure'):
        _group(authority, actor, 'groups.send', params)
    with authority.db._read_ctx() as conn:
        assert conn.execute("SELECT count(*) FROM state_meta WHERE key LIKE 'gateway.hosted.memory-trigger.v1:%'").fetchone()[0] == 0
    monkeypatch.setattr(hosted_rooms, '_insert_event', original)
    # An identical authenticated retry of a legacy event is still unproven.
    _ref, _row, task = submit_task(room, legacy=True)
    replay = _group(authority, actor, 'groups.send', params)['event']
    assert replay['idempotent'] is True
    assert local_memory_context(service, task) is None


def test_scope_follows_only_server_verified_compression_lineage(room):
    from agent.hosted_room_memory import current_hosted_room_memory, hosted_room_memory_scope
    authority, _, _ = room
    db = authority.db
    db.create_session('root', source='local')
    db.create_session('foreign', source='local')
    with hosted_room_memory_scope('root', authority.profile_id, {'version': 1}, resolve_session=authority.logical_owner):
        for parent, child in [('root', 'compressed'), ('compressed', 'compressed-again')]:
            db.publish_compression_child(parent_session_id=parent, child_session_id=child, source='local',
                messages=[{'role': 'user', 'content': 'fixture'}], require_compression_lease=False)
            assert current_hosted_room_memory(child, authority.profile_id) == {'version': 1}
            assert current_hosted_room_memory(child, '/foreign-profile') is None
        assert current_hosted_room_memory('foreign', authority.profile_id) is None
        assert current_hosted_room_memory('missing', authority.profile_id) is None


def test_scope_revokes_copied_background_context_even_on_exception():
    from contextvars import copy_context
    from agent.hosted_room_memory import current_hosted_room_memory, hosted_room_memory_scope
    with pytest.raises(ValueError):
        with hosted_room_memory_scope('session', 'home', {'version': 1}):
            copied = copy_context()
            assert copied.run(current_hosted_room_memory, 'session', 'home') == {'version': 1}
            raise ValueError('end turn')
    assert copied.run(current_hosted_room_memory, 'session', 'home') is None


@pytest.mark.parametrize('automation', [False, True])
def test_real_owner_sockets_carry_attestation_to_sibling_and_source(tmp_path, monkeypatch, automation):
    from tests.gateway.test_hosted_group_members import _gateway, _desktop, _members
    from tests.waiting import wait_until
    from agent.hosted_room_memory import current_hosted_room_memory
    from gateway.session_controls import AuthorityConnection
    from gateway.session_identity import authenticated_subject
    from gateway.runtime_bootstrap import redeem_session_ticket
    seen = []
    async def execute(authority, ref, row):
        seen.append((authority.profile_id, current_hosted_room_memory(ref.session_id, authority.profile_id)))
        return '(pass)'
    monkeypatch.setattr('gateway.session_ingress.execute_admission', execute)
    with _gateway(tmp_path, monkeypatch, served=('atlas',)) as (runner, homes, call):
        desktop = _desktop(runner, call)
        assert 'result' in desktop('default', 'groups.create', room_id='crew', name='Crew',
                                   members=_members('default', 'atlas'))
        if automation:
            store = runner.session_ticket_store
            ticket = store.mint(profile_id=runner.session_authority.profile_id,
                subject=authenticated_subject({'user_id': 'desktop'}), purpose='team-room', scope='host')
            grant = redeem_session_ticket(store, ticket)
            connection = AuthorityConnection(runner.session_authority, object(), {
                'provider': 'local', 'user_id': grant['subject'], 'profile_id': grant['profile_id'],
                'instance_id': grant['instance_id'], 'capabilities': grant['capabilities'],
                'native_bootstrap': True, 'bootstrap_purpose': grant['purpose'], 'profile_scope': grant['scope']})
            def send(event, text):
                return call(connection.dispatch({'id': 1, 'method': 'groups.send', 'params': {
                    'room_id': 'crew', 'event_id': event, 'payload': {'text': text, 'thread_id': 'main'}}}))
        else:
            def send(event, text):
                return desktop('default', 'groups.send', room_id='crew', event_id=event,
                               payload={'text': text, 'thread_id': 'main'})
        for index, profile in enumerate(('atlas', 'default')):
            assert 'result' in send('hello-' + profile, '@' + profile + ' hello')
            wait_until(lambda: len(seen) > index, what='attested member execution', timeout=20)
            home, context = seen[index]
            assert home == str(homes[profile])
            assert context == {'version': 1, 'room_id': 'crew', 'profiles': ['default', 'atlas'],
                               'member_profile': profile, 'trigger': 'team-room' if automation else 'owner'}
        assert current_hosted_room_memory('anything', str(homes['default'])) is None
