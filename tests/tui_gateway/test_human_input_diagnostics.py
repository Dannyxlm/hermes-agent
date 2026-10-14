"""Content-free failures and reconnect scope admission at human-input boundaries."""
import contextlib
import logging
from types import SimpleNamespace

import pytest

from tui_gateway import methods_human_input as hi
from tests.tui_gateway.test_human_input_scopes import host as host, request_body  # noqa: PLC0414 - imported pytest fixture


@pytest.fixture
def rpc_server(host, monkeypatch):
    from hermes_cli import config
    server, _home = host
    server.current_transport = lambda: SimpleNamespace(auth_identity={'provider': 'fixture', 'user_id': 'owner'})
    server._session_profile_runtime_scope = lambda *a, **kw: contextlib.nullcontext()
    server._ok = lambda rid, result: {'id': rid, 'result': result}
    server._err = lambda rid, code, message: {'id': rid, 'error': {'code': code, 'message': message}}
    monkeypatch.setattr(config, 'load_config', lambda: {'cloudseed_mobile': {
        'owner_provider': 'fixture', 'owner_user_id': 'owner'}})
    return server


@pytest.mark.parametrize('exc,code,message,reason', [
    (PermissionError('Runtime profile mismatch'), 4403, 'Human input scope denied', 'Runtime profile mismatch'),
    (PermissionError('Not an ordinary Chat'), 4403, 'Human input scope denied', 'Not an ordinary Chat'),
    (PermissionError('Runtime generation changed'), 4403, 'Human input scope denied', 'Runtime generation changed'),
    (PermissionError('Canonical Bot scope required'), 4403, 'Human input scope denied', 'Canonical Bot scope required'),
    (PermissionError('Approval queue scope is ambiguous'), 4403, 'Human input scope denied', 'Approval queue scope is ambiguous'),
    (PermissionError('Connection request has no chat owner'), 4403, 'Human input scope denied', 'Connection request has no chat owner'),
    (ValueError('DUMMY-PRIVATE-VALUE'), 4400, 'Invalid human input request', 'redacted'),
    (TypeError('DUMMY-PRIVATE-VALUE'), 4400, 'Invalid human input request', 'redacted'),
    (FileNotFoundError('DUMMY-PRIVATE-PATH'), 4400, 'Invalid human input request', 'redacted'),
    (RuntimeError('DUMMY-PRIVATE-VALUE'), 4401, 'Human input unavailable', 'redacted'),
    (OSError('DUMMY-PRIVATE-PATH'), 4401, 'Human input unavailable', 'redacted'),
])
def test_rpc_logs_class_and_fixed_reason_without_changing_error(rpc_server, monkeypatch, caplog, exc, code, message, reason):
    def fail(*args, **kwargs):
        raise exc
    monkeypatch.setattr(hi, 'call', fail)
    with caplog.at_level(logging.INFO, logger=hi.__name__):
        reply = hi._rpc(rpc_server, 'pending', 'rpc-id', request_body())
    assert reply == {'id': 'rpc-id', 'error': {'code': code, 'message': message}}
    record, = [r for r in caplog.records if r.name == hi.__name__]
    assert f'exception_class={type(exc).__name__}' in record.getMessage()
    assert f'reason={reason}' in record.getMessage()
    assert f'code={code}' in record.getMessage()
    assert record.exc_info is None
    assert 'DUMMY-PRIVATE' not in caplog.text
    assert 'saved-connection' not in caplog.text


def test_rpc_scope_denial_records_real_branch(rpc_server, caplog):
    body = {**request_body(), 'session_id': 'stale-runtime'}
    with caplog.at_level(logging.INFO, logger=hi.__name__):
        reply = hi._rpc(rpc_server, 'pending', 'rpc-id', body)
    assert reply['error']['code'] == 4403
    assert 'exception_class=PermissionError' in caplog.text
    assert 'reason=Runtime profile mismatch' in caplog.text
    assert 'stale-runtime' not in caplog.text


@pytest.mark.parametrize('exc', [ValueError('DUMMY-PRIVATE-VALUE'), TypeError('DUMMY-PRIVATE-VALUE'),
    KeyError('DUMMY-PRIVATE-VALUE'), PermissionError('Child request ownership mismatch'),
    RuntimeError('DUMMY-PRIVATE-VALUE')])
def test_compute_swallow_logs_class_without_payload_or_exception_text(host, monkeypatch, caplog, exc):
    from tui_gateway import server, compute_host
    monkeypatch.setattr(server, '_sessions', host[0]._sessions)
    def fail(*args, **kwargs):
        raise exc
    monkeypatch.setattr(hi, 'child_call', fail)
    replies = []
    child = compute_host.ComputeHost.__new__(compute_host.ComputeHost)
    child._reply = lambda *args, **kwargs: replies.append((args, kwargs))
    with caplog.at_level(logging.INFO, logger=compute_host.__name__):
        child._handle_respond({'sid': 'runtime', 'request_id': 'pipe-id',
            'params': {'human_input': {'result': {'value': 'DUMMY-PRIVATE-PAYLOAD'}}}})
    assert replies == [(('respond.error', 'runtime', 'pipe-id'), {'message': 'Human input unavailable'})]
    record, = [r for r in caplog.records if r.name == compute_host.__name__]
    assert f'exception_class={type(exc).__name__}' in record.getMessage()
    assert record.exc_info is None
    assert 'DUMMY-PRIVATE' not in caplog.text


def test_compute_chat_scope_survives_transport_reattach(host, monkeypatch):
    from tui_gateway import server
    from tui_gateway.transport import Transport
    class Peer(Transport):
        def write(self, obj):
            return True
        def close(self):
            pass
    parent, _home = host
    monkeypatch.setattr(server, '_sessions', parent._sessions)
    monkeypatch.setattr(server, '_cancel_ws_orphan_reap', lambda sid: None)
    session = parent._sessions['runtime']
    session.update(_compute_host_turn_id='turn', transport=server._detached_ws_transport)
    scope_before, _ = hi._scope(parent, hi._validate('pending', request_body()), 'owner')
    with server._session_resume_lock, session['history_lock']:
        assert server._reattach_refusal('resume-id', 'runtime', session) is None
        server._rebind_live_transport('runtime', session, Peer())
    assert session['session_key'] == 'same-stored-id'
    assert hi._scope(parent, hi._validate('pending', request_body()), 'owner')[0] == scope_before
    with pytest.raises(PermissionError, match='Runtime profile mismatch'):
        hi._scope(parent, hi._validate('pending', {**request_body(), 'session_id': 'stale-runtime'}), 'owner')
