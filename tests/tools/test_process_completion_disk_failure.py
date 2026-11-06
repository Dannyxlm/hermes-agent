"""Storage failure cannot wedge the in-memory process completion barrier."""
import errno
import io
import json
import threading
from types import SimpleNamespace

import pytest

from tools import process_registry as pr
from tools import process_registry_results as results


@pytest.mark.parametrize('failure', ['enospc', 'deleted_profile', 'corrupt_receipt', 'file_lock'])
def test_failed_result_write_finishes_live_and_preserves_checkpoint(monkeypatch, tmp_path, failure):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    registry = pr.ProcessRegistry()
    pipe = io.StringIO()
    session = pr.ProcessSession(id='proc_disk', command='build', started_at=1,
        notify_on_complete=True, parent_session_id='owner', session_key='owner',
        output_buffer='final output', process=SimpleNamespace(stdout=pipe, stderr=None, stdin=None))
    registry._running[session.id] = session
    registry._write_checkpoint()
    waiter_started = threading.Event()
    result = []
    original_wait = session._completion_event.wait

    def wait(timeout=None):
        waiter_started.set()
        return original_wait(timeout)

    monkeypatch.setattr(session._completion_event, 'wait', wait)
    monkeypatch.setattr(registry, '_reconcile_local_exit', lambda s: None)
    monkeypatch.setattr(registry, '_refresh_detached_session', lambda s: s)
    thread = threading.Thread(target=lambda: result.append(
        registry.wait_for_pending_completions(timeout=5)))
    thread.start()
    assert waiter_started.wait(5)
    path = tmp_path / 'logs/process-results/proc_disk.json'
    with monkeypatch.context() as patch:
        if failure == 'enospc':
            def fail(*a, **k):
                raise OSError(errno.ENOSPC, 'disk full')
            patch.setattr(results, 'atomic_json_write', fail)
        elif failure == 'deleted_profile':
            def fail(*a, **k):
                raise FileNotFoundError('profile deleted')
            patch.setattr('hermes_constants.assert_named_profile_home_live', fail)
        elif failure == 'corrupt_receipt':
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{broken')
        else:
            def fail(*a, **k):
                raise RuntimeError('file lock unavailable')
            patch.setattr('hermes_cli.active_sessions._FileLock', fail)
        try:
            registry._finish_exited(session, 0)
        finally:
            thread.join(6)
    assert not thread.is_alive()
    assert result[0]['completed'] == [session.id]
    assert result[0]['timed_out'] == []
    assert session.id not in registry._running
    assert registry._finished[session.id] is session
    assert pipe.closed and session._completion_event.is_set()
    assert session._result_persistence_error
    event = registry.completion_queue.get_nowait()
    assert event['output'] == 'final output'
    assert event['_process_result_durability_error']
    registry.completion_queue.put(event)
    assert 'final output' in registry.drain_notifications(session_key='owner')[0][1]
    registry._write_checkpoint()  # storage is writable again, including a previously refused lock
    checkpoint = json.loads((tmp_path / 'processes.json').read_text())
    assert checkpoint[0]['session_id'] == session.id
    assert checkpoint[0]['completed_result']['output'] == 'final output'
    assert checkpoint[0]['completed_result']['notification']['status'] == 'unknown'
    assert not registry._move_to_finished(session)
    assert registry.completion_queue.empty()
    # Recovery must not reinterpret a failed durability write as a live PID or blindly replay
    # a notice that the old in-memory consumer may already have accepted.
    if path.exists():
        path.unlink()  # remove the deliberately corrupt fixture, not a production repair
    recovered = pr.ProcessRegistry()
    monkeypatch.setattr(recovered, '_detached_host_fate', lambda *a: pytest.fail('probed completed PID'))
    assert recovered.recover_from_checkpoint() == 0
    receipt = json.loads(path.read_text())
    assert receipt['output'] == 'final output'
    assert receipt['notification']['status'] == 'unknown'
    assert receipt['notification']['admission_id'] is None
    recovered.restore_completions()
    assert recovered.completion_queue.empty()
    assert json.loads((tmp_path / 'processes.json').read_text()) == []


def test_positive_wait_returns_output_even_while_receipt_lock_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    process = pr.ProcessSession(id='proc_wait_disk', command='build', started_at=1,
                                notify_on_complete=True, output_buffer='wait final output')
    registry._running[process.id] = process

    def unavailable(*a, **k):
        raise RuntimeError('file lock unavailable')

    monkeypatch.setattr('hermes_cli.active_sessions._FileLock', unavailable)
    registry._finish_exited(process, 7)
    result = registry.wait(process.id, timeout=2)
    assert result['status'] == 'exited'
    assert result['exit_code'] == 7 and result['output'] == 'wait final output'
    assert registry.is_completion_consumed(process.id)
    assert process._result_persistence_error == 'RuntimeError'


def test_sqlite_launch_write_failure_does_not_block_completion_or_recovery(monkeypatch, tmp_path, caplog):
    import sqlite3
    from hermes_state_registry import acquire, release_or_close
    from tools.process_registry_delivery import notification_state

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    db = acquire(tmp_path / 'state.db')
    try:
        db.record_launch('process:sqlite', kind='process', launcher_profile='default',
                         launcher_session_id='owner', child_profile='default')
        db._execute_write(lambda conn: conn.execute(
            "CREATE TRIGGER refuse_process_finish BEFORE UPDATE ON work_launches "
            "BEGIN SELECT RAISE(ABORT, 'synthetic SQLite write failure'); END"))
        with pytest.raises(sqlite3.IntegrityError, match='synthetic SQLite write failure'):
            db.finish_launch('process:sqlite', 'success')
        registry = pr.ProcessRegistry()
        pipe = io.StringIO()
        process = pr.ProcessSession(id='proc_sqlite', command='build', started_at=1,
            launch_id='process:sqlite', owner_profile_home=str(tmp_path),
            notify_on_complete=True, parent_session_id='owner', session_key='owner',
            output_buffer='SQLite failure final output',
            process=SimpleNamespace(stdout=pipe, stderr=None, stdin=None))
        registry._running[process.id] = process
        registry._finish_exited(process, 0)
        assert process.id not in registry._running and registry._finished[process.id] is process
        assert process._completion_event.is_set() and pipe.closed
        assert not process._result_persistence_error
        assert 'Optional launch metadata skipped' in caplog.text
        event = registry.completion_queue.get_nowait()
        assert notification_state(event)['status'] == 'pending'
        recovered = pr.ProcessRegistry()
        assert recovered.restore_completions() == 1
        [(notice, text)] = recovered.drain_notifications(session_key='owner')
        assert notice['session_id'] == process.id and 'SQLite failure final output' in text
    finally:
        release_or_close(db)


@pytest.mark.parametrize('operation', ['wait', 'read_log'])
def test_positive_result_survives_failure_to_settle_existing_receipt(monkeypatch, tmp_path, operation):
    from tools import process_registry_delivery as delivery
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    process = pr.ProcessSession(id='proc_settle', command='build', started_at=1,
        notify_on_complete=True, output_buffer='already finished output')
    registry._running[process.id] = process
    registry._finish_exited(process, 7)
    event = registry.completion_queue.get_nowait()

    def unavailable(*a, **k):
        raise OSError(errno.ENOSPC, 'synthetic settlement disk full')

    with monkeypatch.context() as patch:
        patch.setattr(delivery, 'atomic_json_write', unavailable)
        result = getattr(registry, operation)(process.id)
    assert result['status'] == 'exited'
    assert result['output'] == 'already finished output'
    if operation == 'wait':
        assert result['exit_code'] == 7
    assert registry.is_completion_consumed(process.id)
    assert delivery.notification_state(event)['status'] == 'pending', 'failed settlement is not a durable ACK'
    # A subsequent successful observation can persist consumption; no live duplicate is emitted.
    registry.read_log(process.id)
    assert delivery.notification_state(event)['status'] == 'terminal'
