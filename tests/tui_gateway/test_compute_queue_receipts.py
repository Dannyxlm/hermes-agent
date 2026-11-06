"""Isolated turns settle only their own queued receipt via correlated host callbacks.

The supervisor's event dispatcher is real; submit is inert and never starts, signals or
reconciles a worker process. Crash frames test uncertainty, not live process recovery.
"""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_queued_prompt_persistence import _busy, _desktop_session
from tui_gateway import server
from tui_gateway.host_supervisor import HostSupervisor, TurnDispatchRefused
from tui_gateway.local_queue_receipts import snapshot

pytest_plugins = ['tests.tui_gateway.test_auto_continue']


@pytest.fixture
def compute_queue(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid, key = _desktop_session(monkeypatch, db)
    session, frames, callbacks, emitted = server._sessions[sid], [], [], []
    supervisor = HostSupervisor(rpc_sink=lambda _: None, autostart=False)

    def submit(frame, *, on_complete):
        frames.append(frame)
        callbacks.append(on_complete)
        supervisor._pending_turns[frame['request_id']] = (sid, on_complete)
        return frame['request_id']

    monkeypatch.setattr(supervisor, 'submit_turn', submit)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *_: supervisor)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {'turn_isolation': True})
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda _s: True)
    monkeypatch.setattr(server, '_compute_host_session_info', lambda _s: {})
    monkeypatch.setattr(server, '_emit', lambda *args: emitted.append(args))
    env = SimpleNamespace(db=db, sid=sid, key=key, session=session, frames=frames,
                          callbacks=callbacks, supervisor=supervisor, emitted=emitted)
    try:
        yield env
    finally:
        server._sessions.pop(sid, None)
        db.close()


def _dispatch(env, text='B'):
    _busy(env.session)
    accepted = server._handle_busy_submit('accept', env.sid, env.session, text, None, queued=True)
    input_id = accepted['result']['input_id']
    assert _receipt(env, input_id)['status'] == 'queued'
    env.session['running'] = False
    assert server._drain_queued_prompt('same-client-id', env.sid, env.session)
    assert _receipt(env, input_id)['status'] == 'transferred'
    return input_id


def _receipt(env, input_id):
    return next(r for r in snapshot(env.db, env.key, server._sessions) if r['input_id'] == input_id)


@pytest.mark.parametrize('done,status,outcome', [
    ({'type': 'turn.end'}, 'terminal', 'completed'),
    ({'type': 'turn.end', 'interrupted': True}, 'terminal', 'interrupted'),
    ({'type': 'turn.end', 'interrupted': True, 'dispatch_refused': True}, 'terminal', 'cancelled'),
    ({'type': 'turn.error', 'reason': 'exception', 'message': 'provider failed'}, 'terminal', 'failed'),
    ({'type': 'turn.error', 'reason': 'crash', 'message': 'host lost'}, 'unknown', None),
])
def test_correlated_completion_commits_the_exact_receipt(compute_queue, done, status, outcome):
    env = compute_queue
    input_id = _dispatch(env)
    frame = env.frames[0]
    assert frame['text'] == 'B' and frame['auto_continue'] is False
    assert env.session['running'] and env.session['_compute_host_turn_id'] == frame['request_id']
    env.supervisor._handle_host_frame({**done, 'sid': env.sid, 'request_id': frame['request_id'],
                                       'session_info_emitted': True})
    receipt = _receipt(env, input_id)
    assert (receipt['status'], receipt['outcome']) == (status, outcome)
    assert not env.session['running'] and not env.session.get('_compute_host_turn_id')
    assert any(t == 'session.info' and p.get('queue_receipts') == [receipt] for t, _s, p in env.emitted)
    server._sessions.pop(env.sid)
    assert snapshot(env.db, env.key, {}) == [receipt]
    assert len(env.frames) == 1  # neither failure nor uncertainty replays B


def test_old_or_mismatched_completion_cannot_settle_the_replacement(compute_queue):
    env = compute_queue
    first = _dispatch(env, 'B')
    frame, complete = env.frames[0], env.callbacks[0]
    # Even calling the registered closure directly cannot bypass wire correlation.
    complete({'type': 'turn.end', 'request_id': 'not-this-turn', 'sid': env.sid})
    assert _receipt(env, first)['status'] == 'transferred'
    assert env.session['_compute_host_turn_id'] == frame['request_id']
    env.supervisor._handle_host_frame({'type': 'turn.end', 'sid': env.sid, 'request_id': frame['request_id']})
    second = _dispatch(env, 'C')
    replacement = env.frames[1]['request_id']
    assert replacement != frame['request_id']
    complete({'type': 'turn.error', 'reason': 'exception', 'sid': env.sid, 'request_id': frame['request_id']})
    assert _receipt(env, first)['outcome'] == 'completed'
    assert _receipt(env, second)['status'] == 'transferred'
    assert env.session['running'] and env.session['_compute_host_turn_id'] == replacement
    env.supervisor._handle_host_frame({'type': 'turn.end', 'sid': env.sid, 'request_id': replacement})
    assert _receipt(env, second)['outcome'] == 'completed'
    assert [f['text'] for f in env.frames] == ['B', 'C']


@pytest.mark.parametrize('uncertain', [False, True])
def test_dispatch_refusal_cancels_but_uncertain_write_never_claims_noninvocation(compute_queue, monkeypatch, uncertain):
    env = compute_queue

    def fail(*_a, **_kw):
        if uncertain:
            raise OSError('flush acknowledgement lost')
        raise TurnDispatchRefused('no host')

    monkeypatch.setattr(env.supervisor, 'submit_turn', fail)
    _busy(env.session)
    accepted = server._handle_busy_submit('B', env.sid, env.session, 'B', None, queued=True)
    env.session['running'] = False
    assert server._drain_queued_prompt('drain', env.sid, env.session)
    receipt = _receipt(env, accepted['result']['input_id'])
    assert (receipt['status'], receipt['outcome']) == (
        ('transferred', 'dispatched') if uncertain else ('terminal', 'cancelled'))
    server._sessions.pop(env.sid)
    cold = snapshot(env.db, env.key, {})[0]
    assert cold['status'] == ('unknown' if uncertain else 'terminal')
    assert cold['outcome'] == (None if uncertain else 'cancelled')
    assert env.frames == []


@pytest.mark.parametrize('failed', [False, True])
def test_child_turn_reports_the_real_runner_terminal_outcome(compute_queue, turn_env, monkeypatch, failed):
    """A handled provider failure still uses turn.end; it must not become completed."""
    import io
    import json
    import threading
    from tui_gateway.compute_host import ComputeHost

    env = compute_queue
    input_id = _dispatch(env)
    invoked = []

    def run(message, **_kwargs):
        invoked.append(message)
        if failed:
            return {'error': 'provider failure', 'final_response': '', 'messages': []}
        return {'final_response': 'answer B', 'messages': []}

    child = {**env.session, 'running': False, 'history_lock': threading.Lock(),
             'agent': SimpleNamespace(session_id=env.key, clear_interrupt=lambda: None, run_conversation=run)}
    monkeypatch.setattr(server, '_wait_agent', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
    monkeypatch.setattr(server, '_start_usage_ticker', lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    parent_sessions = server._sessions
    server._sessions = {env.sid: child}
    try:
        host._run_real_turn(env.frames[0])  # synchronous/inert; no host worker process
    finally:
        server._sessions = parent_sessions
        host.close()
    end = next(json.loads(line) for line in out.getvalue().splitlines() if json.loads(line)['type'] == 'turn.end')
    assert invoked == ['B']
    assert end['terminal_status'] == ('failed' if failed else 'settled')
    env.supervisor._handle_host_frame(end)
    assert _receipt(env, input_id)['outcome'] == ('failed' if failed else 'completed')
