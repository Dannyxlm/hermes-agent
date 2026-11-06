"""Accepted legacy queues get a restart disposition, not just a hidden user row (hunt C4)."""
import pytest
import threading
from types import SimpleNamespace

from hermes_state import SessionDB
from tui_gateway import server
from tests.tui_gateway.test_queued_prompt_persistence import _busy, _desktop_session

pytest_plugins = ['tests.tui_gateway.test_auto_continue']


def test_a_dispatched_inline_queue_commits_its_terminal_receipt_with_the_real_turn(
        monkeypatch, tmp_path, turn_env, marker_home, emits):
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid, key = _desktop_session(monkeypatch, db)
    session, executed = server._sessions[sid], []

    def run(message, **_kwargs):
        executed.append(message)
        return {'final_response': 'answer B', 'messages': []}
    session['agent'] = SimpleNamespace(session_id=key, clear_interrupt=lambda: None, run_conversation=run)
    monkeypatch.setattr(server, '_wait_agent', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
    monkeypatch.setattr(server, '_start_usage_ticker', lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))
    try:
        _busy(session)
        accepted = server._handle_busy_submit('B', sid, session, 'B', None, queued=True)
        session['running'] = False
        assert server._drain_queued_prompt('drain', sid, session) is True
        assert executed == ['B']
        receipt = server._session_control_info(session)['queue_receipts'][0]
        assert receipt['input_id'] == accepted['result']['input_id']
        assert (receipt['status'], receipt['outcome']) == ('terminal', 'completed')
        server._sessions.pop(sid)
        # A cold reader must not turn a committed completion into unknown or replay B.
        from tui_gateway.local_queue_receipts import snapshot
        assert snapshot(db, key, {}) == [receipt]
    finally:
        server._sessions.pop(sid, None)
        db.close()


@pytest.mark.parametrize('compute', [False, True])
def test_restart_cancels_accepted_queue_and_resume_returns_the_same_durable_receipt(monkeypatch, tmp_path, compute):
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    _busy(session)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda _s: compute)
    try:
        accepted = server._handle_busy_submit('B', sid, session, 'B must not vanish', None, queued=True)
        input_id = accepted['result']['input_id']
        # Backend loss destroys the runtime/queue but not the canonical profile store.
        server._sessions.pop(sid)
        db.close()
        db = SessionDB(db_path=tmp_path / 'state.db')
        monkeypatch.setattr(server, '_get_db', lambda: db)
        monkeypatch.setattr(server, '_session_uses_compute_host', lambda _s: False)
        opened = server.handle_request({'id': 'resume', 'method': 'session.resume',
                                       'params': {'session_id': key, 'lazy': True}})
        assert 'error' not in opened, opened
        receipt = next(r for r in opened['result']['info']['queue_receipts'] if r['input_id'] == input_id)
        assert (receipt['status'], receipt['outcome'], receipt['reason']) == ('terminal', 'cancelled', 'backend_restart')
        assert receipt['user'] == 'B must not vanish'
        resumed = server._sessions[opened['result']['session_id']]
        assert not resumed.get('queued_prompt')
        assert server._drain_queued_prompt('again', opened['result']['session_id'], resumed) is False
        assert server._session_control_info(resumed)['queue_receipts'] == [receipt]
        server._sessions.pop(opened['result']['session_id'])
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_stop_racing_a_queue_claim_prevents_dispatch_and_records_cancellation(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid, _key = _desktop_session(monkeypatch, db)
    session, executed = server._sessions[sid], []
    _busy(session)
    try:
        server._handle_busy_submit('B', sid, session, 'B', object(), queued=True)
        session['running'] = False
        stopped = []

        def stop_after_claim(_session):
            if not stopped:
                stopped.append(True)
                server._interrupt_session_turn(sid, session)
            return False
        # This probe is outside the admission/history lock, after the head was claimed.
        monkeypatch.setattr(server, '_session_uses_compute_host', stop_after_claim)
        monkeypatch.setattr(server, '_run_prompt_submit', lambda *a, **kw: executed.append(a))
        assert server._drain_queued_prompt('drain', sid, session) is True
        assert executed == []
        assert server._session_control_info(session)['queue_receipts'][0]['outcome'] == 'cancelled'
    finally:
        server._sessions.pop(sid, None)
        db.close()


@pytest.mark.parametrize('liveness', ['alive', 'unknown', 'dead'])
def test_a_sibling_backend_must_prove_writer_death_before_cancelling(monkeypatch, tmp_path, liveness):
    from tui_gateway import turn_marker
    from tui_gateway.local_queue_receipts import snapshot
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    _busy(session)
    monkeypatch.setattr(turn_marker, '_writer_identity', lambda: {'writer_pid': 987654321, 'writer_start_time': 1.0})
    monkeypatch.setattr(turn_marker, 'marker_writer_state', lambda _r: liveness)
    try:
        server._handle_busy_submit('B', sid, session, 'B', None, queued=True)
        receipt = snapshot(db, key, {})[0]  # another process has no local runtime map
        assert receipt['status'] == ('terminal' if liveness == 'dead' else 'queued')
        with SessionDB(db_path=tmp_path / 'foreign.db') as foreign:
            foreign.create_session(key, 'desktop')
            assert snapshot(foreign, key, {}) == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_deleting_the_session_also_deletes_its_queue_receipt_content(monkeypatch, tmp_path):
    from hermes_state_common import LEGACY_QUEUE_RECEIPT_PREFIX
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    _busy(session)
    try:
        server._handle_busy_submit('B', sid, session, 'private queued text', None, queued=True)
        assert db.delete_session(key)
        with db._read_ctx() as conn:
            assert conn.execute('SELECT key FROM state_meta WHERE key GLOB ?',
                                (LEGACY_QUEUE_RECEIPT_PREFIX + '*',)).fetchall() == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_storage_failure_cannot_acknowledge_an_in_memory_only_queue(monkeypatch, tmp_path):
    import sqlite3
    from tui_gateway import local_queue_receipts
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid, _key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    _busy(session)
    try:
        def disk_failed(*_a):
            raise sqlite3.OperationalError('disk full')
        monkeypatch.setattr(local_queue_receipts, 'accept', disk_failed)
        reply = server._handle_busy_submit('B', sid, session, 'B', None, queued=True)
        assert 'error' in reply
        assert not session.get('queued_prompt')
    finally:
        server._sessions.pop(sid, None)
        db.close()


@pytest.mark.parametrize('boundary', ['reset', 'unreachable_host'])
def test_queue_clearing_boundaries_commit_cancellation_while_runtime_is_live(monkeypatch, tmp_path, boundary):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, key = _desktop_session(monkeypatch, db)
        session = server._sessions[sid]
        try:
            _busy(session)
            accepted = server._handle_busy_submit('B', sid, session, 'B', None, queued=True)
            if boundary == 'reset':
                session['running'] = False
                monkeypatch.setattr(server, '_rebuild_session_agent', lambda *a, **k: session['agent'])
                monkeypatch.setattr(server, '_session_info', lambda *a: {})
                monkeypatch.setattr(server, '_restart_slash_worker', lambda *a: None)
                server._reset_session_agent(sid, session)
            else:
                monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: SimpleNamespace(
                    has_pending_turn=lambda _sid: False))
                server._recover_unreachable_compute_host_turn(sid, session, session['inflight_turn'])
            receipt = server._session_control_info(session)['queue_receipts'][0]
            assert receipt['input_id'] == accepted['result']['input_id']
            assert (receipt['status'], receipt['outcome']) == ('terminal', 'cancelled')
            assert session.get('queued_prompt') is None
            assert not server._drain_queued_prompt('again', sid, session)
            from tui_gateway.local_queue_receipts import snapshot
            assert snapshot(db, key, {}) == [receipt]
        finally:
            server._sessions.pop(sid, None)


def test_dispatch_exception_is_unknown_without_waiting_for_backend_death(monkeypatch, tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, _key = _desktop_session(monkeypatch, db)
        session, invoked = server._sessions[sid], []
        try:
            _busy(session)
            accepted = server._handle_busy_submit('B', sid, session, 'B', None, queued=True)
            later = server._handle_busy_submit('C', sid, session, 'C', None, queued=True, image_paths=[])
            session['running'] = False

            def ambiguous(*a, **k):
                invoked.append(a[3])
                raise RuntimeError('dispatch acknowledgement lost')
            monkeypatch.setattr(server, '_run_prompt_submit', ambiguous)
            assert server._drain_queued_prompt('drain', sid, session)
            receipt = next(r for r in server._session_control_info(session)['queue_receipts']
                           if r['input_id'] == accepted['result']['input_id'])
            assert (receipt['status'], receipt['outcome']) == ('unknown', None)
            assert session['running'] and invoked == ['B']
            assert not server._drain_queued_prompt('again', sid, session)
            assert session['queued_prompt']['_queue_receipt']['input_id'] == later['result']['input_id']
            server._interrupt_session_turn(sid, session)
            receipts = {r['input_id']: r for r in server._session_control_info(session)['queue_receipts']}
            assert receipts[later['result']['input_id']]['outcome'] == 'cancelled'
            assert receipts[accepted['result']['input_id']]['status'] == 'unknown'
        finally:
            server._sessions.pop(sid, None)


def test_queued_terminal_callback_cannot_bypass_strict_nonreplay_marker(
        monkeypatch, tmp_path, turn_env, marker_home, emits):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, _key = _desktop_session(monkeypatch, db)
        session, invoked = server._sessions[sid], []
        session['agent'] = SimpleNamespace(session_id=session['session_key'], clear_interrupt=lambda: None,
            run_conversation=lambda *a, **k: invoked.append(a) or {'final_response': 'answer'})
        monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
        monkeypatch.setattr(server, '_start_usage_ticker', lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))
        record = server.record_turn_start

        def unavailable(*a, **kwargs):
            if kwargs.get('strict'):
                raise OSError('marker not durable')
            return record(*a, **kwargs)
        monkeypatch.setattr(server, 'record_turn_start', unavailable)
        try:
            _busy(session)
            server._handle_busy_submit('B', sid, session, 'B', None, queued=True, auto_continue=False)
            session['running'] = False
            assert server._drain_queued_prompt('drain', sid, session)
            assert invoked == []
            receipt = server._session_control_info(session)['queue_receipts'][0]
            assert (receipt['status'], receipt['outcome']) == ('terminal', 'failed')
        finally:
            server._sessions.pop(sid, None)


def test_stop_durably_cancels_the_queue_before_dropping_it(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    sid, _key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    _busy(session)
    try:
        accepted = server._handle_busy_submit('B', sid, session, 'B', None, queued=True)
        server._interrupt_session_turn(sid, session)
        receipt = server._session_control_info(session)['queue_receipts'][0]
        assert receipt['input_id'] == accepted['result']['input_id']
        assert (receipt['status'], receipt['outcome']) == ('terminal', 'cancelled')
        assert not session.get('queued_prompt')
        assert server._drain_queued_prompt('drain', sid, session) is False
    finally:
        server._sessions.pop(sid, None)
        db.close()
