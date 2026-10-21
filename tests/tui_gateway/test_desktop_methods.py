"""Owner-only Desktop RPCs must not confuse an unfenced viewer with control."""
import contextlib
import threading

import pytest

from tui_gateway import server
from tui_gateway.transport import Transport


class Peer(Transport):
    def __init__(self, identity=None):
        self.auth_identity = identity
        self.frames = []
        self.done = threading.Event()

    def write(self, obj):
        self.frames.append(obj)
        self.done.set()
        return True

    def close(self):
        pass


@pytest.fixture
def rpc(tmp_path, monkeypatch):
    from hermes_cli import config, profiles
    home = tmp_path / 'home'
    home.mkdir()
    secondary = tmp_path / 'secondary'
    secondary.mkdir()
    monkeypatch.setattr(server, '_hermes_home', home)
    monkeypatch.setattr(server, '_profile_home', lambda name: secondary if name == 'secondary' else None)
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda *a, **kw: contextlib.nullcontext())
    monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: home)
    monkeypatch.setattr(config, 'load_config', lambda: {'cloudseed_mobile': {
        'owner_provider': 'fixture', 'owner_user_id': 'owner'}})
    peer = Peer({'provider': 'fixture', 'user_id': 'owner'})

    def call(method, **extra):
        body = {'profile': 'default', 'space': 'studio', 'connection_id': 'connection',
                'installation_id': 'installation', **extra}
        token = server.bind_transport(peer)
        try:
            return server.handle_request({'jsonrpc': '2.0', 'id': 'request', 'method': method, 'params': body})
        finally:
            server.reset_transport(token)
    return call, peer, home


@pytest.mark.parametrize('profile', ['default', 'secondary'])
def test_unavailable_producer_is_not_idle(rpc, profile):
    call, _, home = rpc
    reply = call('desktop.snapshot', profile=profile)
    assert 'result' in reply, reply
    result = reply['result']
    assert result['state'] == 'watch'
    assert result['complete'] is False
    assert result['active'] is None and result['signin'] is None
    assert result['activity'] == [] and result['history'] == []
    assert result['next_cursor'] is None
    assert result['lease'] == {'holder': 'none', 'epoch': 0, 'mine': False}
    assert {'producer', 'history', 'control'} <= set(result['unavailable'])
    assert not (home / 'desktop-spaces.sqlite').exists()


@pytest.mark.parametrize('method,extra', [
    ('desktop.snapshot', {}),
    ('desktop.observe', {'mode': 'watch', 'expected_lease_epoch': 0}),
    ('desktop.action', {'action_id': 'act', 'action': 'take_over', 'expected_revision': 1,
                        'expected_lease_epoch': 0, 'viewer_id': 'invented'}),
    ('desktop.action_status', {'action_id': 'act'}),
])
def test_ids_do_not_authenticate_owner(rpc, method, extra):
    call, peer, _ = rpc
    peer.auth_identity = {'provider': 'fixture', 'user_id': 'stranger'}
    result = call(method, **extra)
    assert result['error']['code'] == 4403


@pytest.mark.parametrize('mode', ['watch', 'control', 'replay'])
def test_no_ticket_from_unfenced_backend(rpc, mode):
    call, _, _ = rpc
    result = call('desktop.observe', mode=mode, expected_lease_epoch=0,
                  recording_id='recording' if mode == 'replay' else None)
    assert result['error']['code'] == 4002
    assert 'ticket' not in str(result)


@pytest.mark.parametrize('action', ['take_over', 'hand_back', 'saved_login', 'paste_login',
                                    'navigate_back', 'open_page'])
def test_input_not_admitted_without_backend_proof(rpc, action):
    call, _, _ = rpc
    fields = {
        'take_over': {'viewer_id': 'viewer'}, 'hand_back': {'viewer_id': 'viewer'},
        'saved_login': {'request_id': 'req', 'request_revision': 1, 'login_handle': 'login'},
        'paste_login': {'viewer_id': 'viewer', 'request_id': 'req', 'request_revision': 1, 'login_handle': 'login'},
        'navigate_back': {'viewer_id': 'viewer'}, 'open_page': {'url': 'https://example.com/'},
    }
    result = call('desktop.action', action_id='act', action=action, expected_revision=1,
                  expected_lease_epoch=0, **fields[action])
    assert result['error']['code'] == 4002


def test_lost_ack_is_unknown_not_permission_to_repeat(rpc):
    call, _, _ = rpc
    result = call('desktop.action_status', action_id='lost')['result']
    assert result['action_id'] == 'lost'
    assert result['status'] == 'unknown'
    assert set(result) == {'action_id', 'status', 'revision', 'lease_epoch', 'server_time'}


@pytest.mark.parametrize('extra', [{'space': 'http://127.0.0.1:9222'}, {'principal': 'owner'},
                                  {'limit': 101}, {'cursor': 'foreign-cursor'}])
def test_scope_and_cursor_are_validated(rpc, extra):
    call, _, _ = rpc
    result = call('desktop.snapshot', **extra)
    assert result['error']['code'] == 4000


def test_snapshot_runs_off_dispatch_thread(rpc):
    _, peer, _ = rpc
    req = {'id': 'async', 'method': 'desktop.snapshot', 'params': {
        'profile': 'default', 'space': 'desktop', 'connection_id': 'conn', 'installation_id': 'install'}}
    assert server.dispatch(req, peer) is None
    assert peer.done.wait(10)
    assert peer.frames[-1]['result']['resource_id'] == 'desktop'
