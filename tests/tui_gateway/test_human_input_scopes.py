"""Exact live runtime admission, using real registry and disposable profile DBs."""
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from hermes_state import SessionDB
from tui_gateway import server_requests as sr
from tui_gateway import methods_human_input as hi


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / 'default'; home.mkdir()
    other = tmp_path / 'other'; other.mkdir()
    for path in (home, other):
        with SessionDB(db_path=path / 'state.db') as db:
            db.create_session('same-stored-id', 'desktop')
    monkeypatch.setenv('HERMES_HOME', str(home))
    server = SimpleNamespace(_hermes_home=home,
        _profile_home=lambda profile: None if profile == 'default' else other,
        _sessions={'runtime': {'profile_home': str(home), 'session_key': 'same-stored-id', 'history_lock': threading.RLock()},
                   'other-runtime': {'profile_home': str(other), 'session_key': 'same-stored-id', 'history_lock': threading.RLock()}},
        _session_uses_compute_host=lambda session: bool(session.get('_compute_host_turn_id')))
    monkeypatch.setattr(hi, '_server', server)
    sr.reset_for_tests()
    sr.bind_sinks(lambda frame: None, lambda *args: None, lambda sid: True)
    yield server, home
    sr.reset_for_tests()


def test_compute_mirror_keeps_each_pending_request_and_exact_cancellation(host, monkeypatch):
    import time
    from tui_gateway import server
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "write_json", lambda frame: True)
    server._sessions['mirror-owner'] = {'history_lock': threading.Lock(), 'session_key': 'stored'}
    frames = [{'jsonrpc': '2.0', 'id': rid, 'method': 'sudo', 'params': {
        'session_id': 'mirror-owner', 'created_at': 1, 'expires_at': time.time() + 300,
        'server_time': time.time(), 'timeout_seconds': 300, 'request_epoch': 'child-epoch',
        'request_revision': 1}} for rid in ('srq-child-first', 'srq-child-second')]
    for frame in frames:
        server._relay_compute_host_rpc(frame)
    assert {req['id'] for req in server._open_requests('mirror-owner')} == {frame['id'] for frame in frames}
    server._relay_compute_host_rpc({'method': 'event', 'params': {'type': 'request.cancel',
        'session_id': 'mirror-owner', 'payload': {'id': frames[0]['id'], 'method': 'sudo', 'reason': 'resolved'}}})
    assert [req['id'] for req in server._open_requests('mirror-owner')] == [frames[1]['id']]


def request_body():
    return dict(profile='default', connection_id='saved-connection', installation_id='install',
        destination_kind='chat', stored_session_id='same-stored-id', session_id='runtime')


def test_default_no_home_override_and_cross_profile_scope(host):
    results = []
    sr.send_async('approval', 'runtime', {'request_id': 'queue', 'choices': ['once', 'deny']}, results.append, timeout=300)
    pending = hi.call('pending', request_body(), principal='owner')
    assert pending['complete'] and len(pending['requests']) == 1
    request = pending['requests'][0]
    body = {**request_body(), 'request_id': request['id'], 'method': request['method'],
            'request_epoch': request['params']['request_epoch'], 'request_revision': request['params']['request_revision'],
            'action_id': 'tap-1', 'result': {'choice': 'deny', 'reason': 'Read only instead'}}
    with pytest.raises(PermissionError):
        hi.call('respond', {**body, 'profile': 'other'}, principal='owner')
    assert not results
    # The legacy queue cannot disambiguate equal durable IDs across profiles.
    with pytest.raises(PermissionError):
        hi.call('respond', body, principal='owner')
    del host[0]._sessions['other-runtime']
    assert hi.call('respond', body, principal='owner')['status'] == 'accepted'
    status = {k: v for k, v in body.items() if k != 'result'}
    assert hi.call('action-status', status, principal='owner')['status'] == 'accepted'
    assert hi.call('action-status', status, principal='different-owner')['status'] == 'unknown'
    del host[0]._sessions['runtime']
    assert hi.call('action-status', status, principal='owner')['status'] == 'accepted'
    assert len(results) == 1


def test_pending_secure_metadata_drops_defaults(host):
    sr.send_async('secret', 'runtime', {'env_var': 'DUMMY', 'prompt': 'Enter',
        'metadata': {'default': 'DUMMY-SECRET'}}, lambda result: None, timeout=300)
    pending = hi.call('pending', request_body(), principal='owner')
    assert 'DUMMY-SECRET' not in repr(pending)


def test_detached_runtime_never_created_and_compute_unavailable_is_partial(host):
    server, _home = host
    server._sessions['runtime']['_compute_host_turn_id'] = 'turn'
    def unavailable():
        raise RuntimeError('child unavailable')
    server._get_compute_host_supervisor = unavailable
    pending = hi.call('pending', request_body(), principal='owner')
    assert not pending['complete']
    assert pending['unavailable'] == ['compute_child']
    assert server._sessions.keys() == {'runtime', 'other-runtime'}


def test_compute_owned_action_uses_private_dispatch_and_never_starts_child(host, monkeypatch):
    from tui_gateway import server as gateway
    from tui_gateway.compute_host import ComputeHost
    server, _home = host
    monkeypatch.setattr(hi, '_server', server)
    monkeypatch.setattr(gateway, '_sessions', server._sessions)
    server._sessions['runtime']['_compute_host_turn_id'] = 'turn'
    replies = []
    child = ComputeHost.__new__(ComputeHost)
    child._reply = lambda kind, sid, request_id, **extra: replies.append({'type': kind, **extra})
    class Pipe:
        def respond(self, sid, params, *, start_if_missing):
            assert start_if_missing is False
            child._handle_respond({'sid': sid, 'request_id': 'pipe-id', 'params': params})
            return replies.pop()
    server._compute_host_supervisor = Pipe()
    seen = []
    sr.bind_sinks(lambda frame: None, lambda *args: None, lambda sid: True)
    sr.send_async('vault.code', 'runtime', {'site': 'Example'}, seen.append, timeout=300)
    request = hi.call('pending', request_body(), principal='owner')['requests'][0]
    body = {**request_body(), 'request_id': request['id'], 'method': 'vault.code',
            'request_epoch': request['params']['request_epoch'], 'request_revision': 1,
            'action_id': 'child-tap', 'result': {'value': 'DUMMY-OTP'}}
    assert hi.call('respond', body, principal='owner')['status'] == 'accepted'
    assert hi.call('respond', body, principal='owner')['status'] == 'accepted'
    assert seen == [{'value': 'DUMMY-OTP'}]
    body.pop('result')
    assert hi.call('action-status', body, principal='owner')['status'] == 'accepted'
    assert 'DUMMY-OTP' not in repr(sr._receipts)


def test_unknown_process_and_payload_are_fail_closed(host, monkeypatch):
    monkeypatch.setattr(hi, '_server', None)
    with pytest.raises(RuntimeError):
        hi.call('pending', request_body(), principal='owner')


@pytest.mark.parametrize("hosted", [False, True])
def test_real_approval_producer_through_rest_receipt(host, monkeypatch, hosted):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.cloudseed_mobile.dashboard import plugin_api as api
    from tools import approval, approval_gateway_wait as wait
    from tui_gateway import server as gateway
    from tui_gateway import methods_human_input as loaded_hi
    server, _home = host
    del server._sessions['other-runtime']
    monkeypatch.setattr(loaded_hi, '_server', server)
    monkeypatch.setattr(gateway, '_sessions', server._sessions)
    monkeypatch.setattr(approval, '_gateway_queues', {})
    monkeypatch.setattr(wait._ctx, '_get_approval_timeout', lambda: 300)
    monkeypatch.setattr(wait._ctx, '_fire_approval_hook', lambda *a, **kw: None)
    monkeypatch.setattr(api, 'profile_id', lambda: 'default')
    published = threading.Event()
    sr.bind_sinks(lambda frame: published.set(), lambda *a: None, lambda sid: True)
    result = {}
    worker = threading.Thread(target=lambda: result.update(wait._await_gateway_decision(
        'same-stored-id', lambda data: gateway._emit_approval_request('runtime', data),
        {'command': 'dummy-read-only', 'allow_session': False})), daemon=True)
    app = FastAPI()
    if hosted:
        from hermes_cli import web_server_dashboard as dashboard
        monkeypatch.setattr(dashboard, '_hosted_plugin_for_request', lambda name: (object(), '/fixture', '/fixture/api.py'))
        dashboard._mount_hosted_plugin_api(app, {'name': 'cloudseed_mobile'}, 'api.py')
    else:
        app.include_router(api.router, prefix='/api/plugins/cloudseed_mobile')
    app.dependency_overrides[api.owner] = lambda: 'owner'
    worker.start()
    try:
        assert published.wait(10)
        with TestClient(app) as client:
            response = client.post('/api/plugins/cloudseed_mobile/human-input/pending', json=request_body())
            assert response.status_code == 200
            request = response.json()['requests'][0]
            entry = approval._gateway_queues['same-stored-id'][0]
            assert request['params']['expires_at'] == entry.data['expires_at']
            assert request['params']['timeout_seconds'] == 300
            body = {**request_body(), 'request_id': request['id'], 'method': 'approval',
                'request_epoch': request['params']['request_epoch'], 'request_revision': 1,
                'action_id': 'rest-tap', 'result': {'choice': 'deny', 'reason': 'Use a read-only alternative'}}
            assert client.post('/api/plugins/cloudseed_mobile/human-input/respond', json=body).json()['status'] == 'accepted'
            body.pop('result')
            assert client.post('/api/plugins/cloudseed_mobile/human-input/action-status', json=body).json()['status'] == 'accepted'
        worker.join(10)
        assert not worker.is_alive()
        assert result['choice'] == 'deny' and result['reason'] == 'Use a read-only alternative'
        assert not sr.open_requests('runtime')
    finally:
        sr.cancel('runtime')
        worker.join(10)


def test_timeout_policy_reads_active_profile_yaml(host):
    from hermes_cli.config import DEFAULT_CONFIG, _KNOWN_ROOT_KEYS, atomic_config_write
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tui_gateway.human_input import human_timeout
    assert DEFAULT_CONFIG['human_input']['timeout'] == 300
    assert 'human_input' in _KNOWN_ROOT_KEYS
    atomic_config_write(host[1] / 'config.yaml', {'human_input': {'timeout': 241}})
    other = host[1] / 'profiles' / 'other'
    other.mkdir(parents=True)
    (other / 'profile.yaml').write_text('name: other\n', encoding='utf-8')
    atomic_config_write(other / 'config.yaml', {'human_input': {'timeout': 179}})
    for home, expected in ((host[1], 241), (other, 179)):
        token = set_hermes_home_override(home)
        try:
            assert human_timeout() == expected
        finally:
            reset_hermes_home_override(token)


def test_sessionless_install_is_profile_not_selected_chat_owned(host):
    sr.send_async('display.install.sudo', '', {'profile_key': str(host[1])}, lambda result: None, timeout=300)
    body = dict(profile='default', connection_id='saved-connection', installation_id='install',
                destination_kind='connection', session_id='')
    pending = hi.call('pending', body, principal='owner')
    assert len(pending['requests']) == 1
    assert pending['requests'][0]['params']['session_id'] == ''
