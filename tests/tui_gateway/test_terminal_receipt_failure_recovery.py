"""Terminal durability faults must preserve controls, answers and compute cleanup."""
import sqlite3
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_compute_queue_receipts import _dispatch
from tests.tui_gateway.test_compute_queue_receipts import compute_queue as compute_queue  # noqa: PLC0414 -- pytest fixture re-export
from tests.tui_gateway.test_queued_prompt_persistence import _busy, _desktop_session
from tui_gateway import local_queue_receipts, server as _server

server: Any = _server

pytest_plugins = ['tests.tui_gateway.test_auto_continue']


def _locked(*args, **kwargs):
    raise sqlite3.OperationalError('database is locked')


@pytest.mark.parametrize('fault', ['disposition', 'snapshot'])
def test_compute_completion_releases_slot_despite_receipt_failure(compute_queue, monkeypatch, fault):
    env = compute_queue
    input_id = _dispatch(env)
    frame = env.frames[0]
    if fault == 'disposition':
        assert server._handle_busy_submit('C', env.sid, env.session, 'follower', None, queued=True)['result']['status'] == 'queued'
    monkeypatch.setattr(local_queue_receipts, fault, _locked)
    env.supervisor._handle_host_frame({'type': 'turn.end', 'sid': env.sid, 'request_id': frame['request_id'],
                                      'session_info_emitted': True})
    assert not env.supervisor.has_pending_turn(env.sid)
    from tests.waiting import wait_until
    assert wait_until(lambda: not env.session['running'], timeout=10)
    for worker in threading.enumerate():
        if worker.name == f'compute-host-complete-{env.sid}':
            worker.join(10)
            assert not worker.is_alive()
    if fault == 'disposition':
        assert len(env.frames) == 1, 'an unresolved receipt released its FIFO follower'
        assert server._drain_queued_prompt('again', env.sid, env.session) is False
        receipts = server._session_control_info(env.session)['queue_receipts']
        assert next(row for row in receipts if row['input_id'] == input_id)['status'] == 'unknown'


def test_compute_receipt_projection_does_not_occupy_frame_reader(compute_queue, monkeypatch):
    env = compute_queue
    _dispatch(env)
    frame = env.frames[0]
    entered, release, returned = (threading.Event() for _ in range(3))

    def blocked_info(session):
        entered.set()
        assert release.wait(10)
        return {}

    monkeypatch.setattr(server, '_session_control_info', blocked_info)

    def receive():
        env.supervisor._handle_host_frame({'type': 'turn.end', 'sid': env.sid,
            'request_id': frame['request_id'], 'session_info_emitted': True})
        returned.set()

    reader = threading.Thread(target=receive)
    reader.start()
    try:
        assert entered.wait(10)
        assert returned.wait(2), 'owner lookup blocked the compute-host frame reader'
        assert not env.session['running']
        got = env.session['history_lock'].acquire(timeout=2)
        assert got, 'projection held the history lock'
        if got:
            env.session['history_lock'].release()
    finally:
        release.set()
        reader.join(10)
        for worker in threading.enumerate():
            if worker.name == f'compute-host-complete-{env.sid}':
                worker.join(10)
                assert not worker.is_alive()


@pytest.mark.parametrize('replacement_completing', [False, True])
def test_delayed_compute_settlement_cannot_clear_a_replacement_turn(compute_queue, monkeypatch, replacement_completing):
    env = compute_queue
    _dispatch(env)
    frame = env.frames[0]
    entered, release = threading.Event(), threading.Event()
    disposition = local_queue_receipts.disposition

    def blocked_disposition(*args):
        entered.set()
        assert release.wait(10)
        return disposition(*args)

    monkeypatch.setattr(local_queue_receipts, 'disposition', blocked_disposition)
    env.callbacks[0]({'type': 'turn.end', 'sid': env.sid, 'request_id': frame['request_id']})
    try:
        assert entered.wait(10)
        with env.session['history_lock']:
            env.session['_compute_host_turn_id'] = 'replacement-dispatch'
            env.session['running'] = True
            env.session['inflight_turn'] = {'user': 'replacement input'}
            if replacement_completing:
                env.session['_compute_host_completing'] = 'replacement-dispatch'
    finally:
        release.set()
        for worker in threading.enumerate():
            if worker.name == f'compute-host-complete-{env.sid}':
                worker.join(10)
                assert not worker.is_alive()
    assert env.session['running']
    assert env.session['_compute_host_turn_id'] == 'replacement-dispatch'
    assert env.session['inflight_turn']['user'] == 'replacement input'
    assert env.session.get('_compute_host_completing') == ('replacement-dispatch' if replacement_completing else None)


@pytest.mark.parametrize('compute', [False, True])
def test_stop_control_failure_still_settles_its_cancelled_queue(compute_queue, monkeypatch, compute):
    env = compute_queue
    _busy(env.session)
    accepted = server._handle_busy_submit('B', env.sid, env.session, 'cancelled input', None, queued=True)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda _: compute)

    def unavailable(*args, **kwargs):
        raise OSError('synthetic unavailable control')

    if compute:
        monkeypatch.setattr(env.supervisor, 'interrupt', unavailable)
    else:
        import agent.interrupt_compat as interrupts
        env.session['_run_thread'] = SimpleNamespace(is_alive=lambda: True)
        monkeypatch.setattr(interrupts, 'request_hard_interrupt', unavailable)
    with pytest.raises(OSError, match='synthetic unavailable control'):
        server._interrupt_session_turn(env.sid, env.session)
    receipt = next(row for row in server._session_control_info(env.session)['queue_receipts']
                   if row['input_id'] == accepted['result']['input_id'])
    assert (receipt['status'], receipt['outcome']) == ('terminal', 'cancelled')
    assert not env.session.get('queued_prompt')


def test_local_stop_reaches_agent_before_blocked_receipt_io(monkeypatch, tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, _key = _desktop_session(monkeypatch, db)
        session = server._sessions[sid]
        entered, release, stopped = (threading.Event() for _ in range(3))
        import agent.interrupt_compat as ic
        monkeypatch.setattr(ic, 'request_hard_interrupt', lambda agent: stopped.set())
        monkeypatch.setattr(server, '_session_uses_compute_host', lambda session: False)
        _busy(session)
        session['_run_thread'] = SimpleNamespace(is_alive=lambda: True)
        server._handle_busy_submit('B', sid, session, 'B', None, queued=True)
        disposition = local_queue_receipts.disposition

        def slow_disposition(*args):
            entered.set()
            assert release.wait(10)
            return disposition(*args)

        monkeypatch.setattr(local_queue_receipts, 'disposition', slow_disposition)
        worker = threading.Thread(target=server._interrupt_session_turn, args=(sid, session))
        worker.start()
        try:
            assert entered.wait(10)
            assert stopped.is_set(), 'receipt I/O postponed Stop'
            got = session['history_lock'].acquire(timeout=2)
            assert got, 'receipt I/O held the history lock'
            if got:
                session['history_lock'].release()
        finally:
            release.set()
            worker.join(10)
            server._sessions.pop(sid, None)


def test_local_stop_reaches_agent_despite_receipt_failure(monkeypatch, tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, _key = _desktop_session(monkeypatch, db)
        session = server._sessions[sid]
        hard = []
        import agent.interrupt_compat as ic
        monkeypatch.setattr(ic, 'request_hard_interrupt', lambda agent: hard.append(agent))
        monkeypatch.setattr(server, '_session_uses_compute_host', lambda session: False)
        try:
            _busy(session)
            session['_run_thread'] = SimpleNamespace(is_alive=lambda: True)
            assert server._handle_busy_submit('B', sid, session, 'B', None, queued=True)['result']['status'] == 'queued'
            monkeypatch.setattr(local_queue_receipts, 'disposition', _locked)
            server._interrupt_session_turn(sid, session)
            assert hard, 'Stop never reached the running agent'
        finally:
            server._sessions.pop(sid, None)


@pytest.mark.parametrize('fault', ['disposition', 'projection'])
def test_completed_queued_turn_keeps_answer_when_receipt_or_projection_fails(
        monkeypatch, tmp_path, turn_env, marker_home, emits, fault):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, key = _desktop_session(monkeypatch, db)
        session = server._sessions[sid]
        session['agent'] = SimpleNamespace(session_id=key, clear_interrupt=lambda: None,
            run_conversation=lambda message, **kwargs: {'final_response': 'answer B', 'messages': []})
        monkeypatch.setattr(server, '_wait_agent', lambda *a, **kw: None)
        monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
        monkeypatch.setattr(server, '_start_usage_ticker',
                            lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))
        real = local_queue_receipts.disposition

        def fail_terminal(db, envelope, outcome):
            if outcome == 'dispatched':
                return real(db, envelope, outcome)
            raise sqlite3.OperationalError('database is locked')

        try:
            _busy(session)
            server._handle_busy_submit('B', sid, session, 'B', None, queued=True)
            session['running'] = False
            if fault == 'disposition':
                monkeypatch.setattr(local_queue_receipts, 'disposition', fail_terminal)
            else:
                monkeypatch.setattr(server, '_session_control_info', _locked)
            assert server._drain_queued_prompt('drain', sid, session) is True
            completes = [p for e, _s, p in emits if e == 'message.complete']
            assert completes and completes[-1].get('text') == 'answer B'
            assert completes[-1].get('status') != 'error'
        finally:
            server._sessions.pop(sid, None)
