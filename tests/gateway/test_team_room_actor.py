"""Meeting prompt authorship is attested by the private bootstrap, not RPC text/fields."""
from types import SimpleNamespace

import pytest

from gateway.runtime_bootstrap import TicketStore
from gateway.session_controls import AuthorityConnection
from gateway.session_identity import authenticated_subject
from tests.gateway.test_hosted_group_members import _gateway


ACTOR = {'kind': 'user', 'id': 'team-room', 'display_name': 'Team room'}


def test_team_room_ticket_has_explicit_origin_and_no_approval_or_operator_authority():
    store = TicketStore('instance', frozenset({'profile'}))
    token = store.mint(profile_id='profile', subject='fixture', purpose='team-room', scope='host')
    with pytest.raises(PermissionError):
        store.redeem(token, profile_id='profile', purpose='interactive')
    grant = store.redeem(token, profile_id='profile', purpose='team-room')
    assert grant['purpose'] == 'team-room' and grant['scope'] == 'host'
    assert {'session:read', 'session:submit', 'session:control'} <= grant['capabilities']
    assert not ({'session:operator', 'session:approve', 'session:respond'} & grant['capabilities'])
    with pytest.raises(PermissionError):
        store.redeem(token, profile_id='profile', purpose='team-room')


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose', ['interactive', 'team-room'])
async def test_websocket_gate_stamps_only_redeemed_ticket_origin(tmp_path, monkeypatch, purpose):
    from gateway.run_api import GatewayRuntimeAPI
    from hermes_cli import web_server as web
    tickets = TicketStore('owner', frozenset({str(tmp_path)}))
    ticket = tickets.mint(profile_id=str(tmp_path), subject='fixture', purpose=purpose, scope='host')
    authority = SimpleNamespace(db=SimpleNamespace(db_path=tmp_path / 'state.db'), profile_id=str(tmp_path), runner=None)
    runner = SimpleNamespace(_draining=False, session_runtime_descriptor={'state': 'ready'},
                             session_authority=authority, session_ticket_store=tickets)
    seen = []
    async def fallback(*args):
        pytest.fail('A valid private ticket entered remote fallback')
    async def handle(ws, **kwargs):
        seen.append(kwargs)
    monkeypatch.setattr('tui_gateway.ws.handle_ws', handle)
    monkeypatch.setattr(web, '_DASHBOARD_EMBEDDED_CHAT_ENABLED', True)
    incoming = iter([{'type': 'websocket.connect'}])
    async def receive():
        return next(incoming)
    async def send(value):
        pass
    scope = {'type': 'websocket', 'path': '/api/ws', 'scheme': 'ws', 'query_string': b'',
        'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 9999),
        'headers': [(b'sec-websocket-protocol', ('hermes-gateway-v1, hermes-gateway-ticket.' + ticket).encode())],
        'subprotocols': ['hermes-gateway-v1', 'hermes-gateway-ticket.' + ticket]}
    await GatewayRuntimeAPI(fallback, runner, web.app)(scope, receive, send)
    assert len(seen) == 1
    identity = seen[0]['auth_identity']
    assert identity['native_bootstrap'] is True
    assert identity['bootstrap_purpose'] == purpose
    assert identity['profile_scope'] == 'host'
    assert seen[0]['operator'] is (purpose == 'interactive')


def test_room_actor_is_derived_from_attested_connection_not_remote_params(tmp_path, monkeypatch):
    subject = authenticated_subject({'provider': 'basic', 'user_id': 'fixture-owner'})
    with _gateway(tmp_path, monkeypatch, served=('peer',)) as (runner, _homes, call):
        authority = runner.session_authority
        # Stop execution while testing admission/log identity, not a model provider.
        assert authority.hosted_room_service.stop(timeout=2)
        human_identity = {'provider': 'basic', 'user_id': 'fixture-owner', 'profile_scope': 'host'}
        human = AuthorityConnection(authority, object(), human_identity)
        automation = AuthorityConnection(authority, object(), {
            'provider': 'local', 'user_id': subject, 'profile_id': authority.profile_id,
            'instance_id': authority.instance_id, 'capabilities': ['session:read', 'session:submit', 'session:control'],
            'native_bootstrap': True, 'bootstrap_purpose': 'team-room', 'profile_scope': 'host'})
        remote = AuthorityConnection(authority, object(), {**human_identity, 'bootstrap_purpose': 'team-room'})

        def request(connection, method, **params):
            return call(connection.dispatch({'id': 1, 'method': method, 'params': params}))

        # Metadata setup can run without the worker, then enable its RPC-ready status only;
        # no background thread is started and every accepted send is read directly from storage.
        created = request(human, 'groups.create', room_id='meeting', name='Meeting', members=[
            {'member_id': 'default', 'profile': 'default', 'handle': 'ava'},
            {'member_id': 'peer', 'profile': 'peer', 'handle': 'peer'}],
            meeting={'kind': 'standup', 'schedule_local': '08:15', 'timezone': 'UTC'})
        assert 'result' in created, created
        monkeypatch.setattr(authority.hosted_room_service.runtime, 'status', lambda: {'running': True, 'stopping': False})
        monkeypatch.setattr(authority.hosted_room_service, 'prepare_room', lambda binding: None)
        payload = {'thread_id': 'main', 'text': 'Fixture prompt'}
        for connection, key, expected in [(automation, 'automation', ACTOR),
                                          (human, 'human', {'kind': 'user', 'id': 'desktop'}),
                                          (remote, 'remote', {'kind': 'user', 'id': 'desktop'})]:
            result = request(connection, 'groups.send', room_id='meeting', event_id=key, payload=payload)
            assert result['result']['event']['actor'] == expected
            assert result['result']['event']['kind'] == 'message.user'
        replay = request(automation, 'groups.send', room_id='meeting', event_id='automation', payload=payload)
        assert replay['result']['event']['idempotent'] is True
        assert replay['result']['event']['actor'] == ACTOR
        conflict = request(human, 'groups.send', room_id='meeting', event_id='automation', payload=payload)
        assert 'error' in conflict
        for extra in ({'actor': ACTOR}, {'origin': 'team-room'}, {'bootstrap_purpose': 'team-room'}):
            refused = request(human, 'groups.send', room_id='meeting', event_id='forged', payload=payload, **extra)
            assert 'error' in refused
        refused = request(human, 'groups.send', room_id='meeting', event_id='forged-payload',
                          payload={**payload, 'actor': ACTOR})
        assert 'error' in refused
        events = request(human, 'groups.log', room_id='meeting')['result']['events']
        assert [e['actor'] for e in events if e['kind'] == 'message.user'] == [
            ACTOR, {'kind': 'user', 'id': 'desktop'}, {'kind': 'user', 'id': 'desktop'}]
