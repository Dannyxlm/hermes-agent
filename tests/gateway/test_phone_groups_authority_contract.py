"""The phone's authority socket, not the legacy groups dispatcher, satisfies its room gates."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.gateway.test_hosted_group_members import _gateway, _members
from tests.waiting import wait_until
from tui_gateway.ws import _authority_connection, _dispatch_request


@pytest.fixture
def phone(tmp_path, monkeypatch):
    from gateway import session_finite
    ran = []

    async def execute(authority, ref, row):
        ran.append(ref.session_id)
        return 'Fixture phone reply.'

    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with _gateway(tmp_path, monkeypatch, served=('peer',)) as (runner, _homes, call):
        authority = runner.session_authority
        # run_api places this exact authority in the phone socket's scope. No legacy
        # server.dispatch fallback is allowed: every verb below must be authority-owned.
        ws = SimpleNamespace(scope={'hermes.session_authority': authority})
        transport = SimpleNamespace(desktop_local=False)
        connection = _authority_connection(ws, transport, {'user_id': 'fixture-phone', 'profile_scope': 'host'}, False)
        assert connection.authority is authority
        monkeypatch.setattr('tui_gateway.ws.server.dispatch', lambda *args: pytest.fail('legacy fallback'))

        def rpc(method, **params):
            request = {'id': 1, 'method': method, 'params': {'profile': 'default', **params}}
            response = call(_dispatch_request(connection, request, method, transport))
            return json.loads(json.dumps(response))  # The actual JSON value types the app decodes.

        yield rpc, authority.hosted_room_service, ran


def _result(reply):
    assert 'result' in reply, reply
    return reply['result']


def _create(rpc, *, meeting=None):
    return _result(rpc('groups.create', room_id='phone-room', name='Phone room',
                       members=_members('default', 'peer'), meeting=meeting))['room']


@pytest.mark.parametrize('meeting', [None, {'kind': 'standup', 'schedule_local': '08:15', 'timezone': 'UTC'}])
def test_phone_authority_read_send_gates_and_epoch_contract(phone, meeting):
    rpc, _service, ran = phone
    caps = _result(rpc('groups.capabilities'))
    assert caps['driver'] is True
    assert set(caps['features']) == {
        'authority_epoch', 'room_identity', 'monotonic_log', 'idempotent_send', 'replayable_disband'}
    assert 'groups.send' in caps['methods']
    room = _create(rpc, meeting=meeting)
    state = _result(rpc('groups.state', room_id=room['room_id']))['room']
    epoch = state['authority_epoch']
    assert type(epoch) is int and epoch >= 1
    expected = {'expected_authority_gateway_id': state['authority_gateway_id'], 'expected_authority_epoch': epoch}
    params = {'room_id': room['room_id'], 'event_id': 'phone-send',
              'payload': {'thread_id': 'phone-generated-thread', 'text': '@peer fixture question'}, **expected}
    sent = _result(rpc('groups.send', **params))
    assert sent['accepted'] is True and sent['client_event_id'] == 'phone-send'
    event = sent['event']
    assert type(event['authority_epoch']) is int and event['authority_epoch'] == epoch
    assert event['actor'] == {'kind': 'user', 'id': 'desktop'}
    assert event['payload']['thread_id'] == ('main' if meeting else 'phone-generated-thread')
    retried = _result(rpc('groups.send', **params))['event']
    assert retried['idempotent'] is True
    assert (retried['event_id'], retried['seq'], retried['authority_epoch']) == (
        event['event_id'], event['seq'], epoch)
    conflict = rpc('groups.send', **{**params, 'payload': {**params['payload'], 'text': 'Different content'}})
    assert 'error' in conflict

    def settled():
        return any(e['kind'] == 'room.activity' and e['payload'].get('status') == 'settled'
                   for e in _result(rpc('groups.log', room_id=room['room_id']))['events'])

    wait_until(settled, what='phone authority member reply and settled receipt')
    _result(rpc('groups.rename', room_id=room['room_id'], event_id='phone-rename', name='Renamed phone room'))
    log = _result(rpc('groups.log', room_id=room['room_id']))
    assert {'message.user', 'message.member', 'room.activity', 'room.renamed'} <= {e['kind'] for e in log['events']}
    assert all(type(e['authority_epoch']) is int and e['authority_epoch'] >= 1 for e in log['events'])
    assert all(e['authority_epoch'] == epoch for e in log['events'])
    assert sum(e['event_id'] == event['event_id'] for e in log['events']) == 1
    assert len(ran) == 1


def test_phone_expected_authority_is_validated_and_stale_sends_do_not_append(phone):
    rpc, _service, ran = phone
    room = _create(rpc)
    gateway, epoch = room['authority_gateway_id'], room['authority_epoch']
    invalid = [
        {'expected_authority_gateway_id': gateway}, {'expected_authority_epoch': epoch},
        {'expected_authority_gateway_id': None, 'expected_authority_epoch': epoch},
        {'expected_authority_gateway_id': '', 'expected_authority_epoch': epoch},
        *[{'expected_authority_gateway_id': gateway, 'expected_authority_epoch': value}
          for value in [None, True, '1', 0, -1]],
    ]
    stale = [{'expected_authority_gateway_id': 'another-gateway', 'expected_authority_epoch': epoch},
             {'expected_authority_gateway_id': gateway, 'expected_authority_epoch': epoch + 1}]
    for expectation in invalid + stale:
        reply = rpc('groups.send', room_id=room['room_id'], event_id='rejected',
                    payload={'thread_id': 'main', 'text': '@peer do not run'}, **expectation)
        assert reply['error']['message'] == ('invalid_params' if expectation in invalid else 'authority_conflict')
    assert not _result(rpc('groups.log', room_id=room['room_id']))['events']
    assert not ran
    # Backward compatibility: old callers need not supply either expected coordinate.
    _result(rpc('groups.send', room_id=room['room_id'], event_id='old-client', payload={'thread_id': 'main', 'text': '@peer hello'}))


def test_phone_expected_authority_fences_a_change_between_read_and_append(phone, monkeypatch):
    rpc, service, ran = phone
    room = _create(rpc)
    original = service._owned_authority

    def competing_write(room_id):
        coordinates = original(room_id)
        # Fault injection: another authority epoch wins immediately after the service's read.
        with sqlite3.connect(service.db_path) as conn:
            conn.execute('UPDATE hosted_rooms SET authority_epoch=authority_epoch+1 WHERE room_id=?', (room_id,))
        return coordinates

    monkeypatch.setattr(service, '_owned_authority', competing_write)
    reply = rpc('groups.send', room_id=room['room_id'], event_id='raced', payload={'thread_id': 'main', 'text': '@peer do not run'},
                expected_authority_gateway_id=room['authority_gateway_id'],
                expected_authority_epoch=room['authority_epoch'])
    assert reply['error']['message'] == 'authority_conflict'
    assert not _result(rpc('groups.log', room_id=room['room_id']))['events']
    assert not ran
