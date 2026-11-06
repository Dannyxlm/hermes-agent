"""Completed-result persistence must retain the notification across every publication cut."""
import json

import pytest

from tools import process_registry as pr


class Crash(BaseException):
    pass


@pytest.mark.parametrize('boundary', ['result', 'checkpoint', 'enqueue', 'queued'])
def test_completed_notification_survives_publication_crash(tmp_path, monkeypatch, boundary):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(id='proc_crash', command='build', started_at=1.0,
                                session_key='owner', parent_session_id='owner',
                                notify_on_complete=True, output_buffer='actual final output')
    registry._running[session.id] = session
    registry._write_checkpoint()
    if boundary == 'result':
        save = pr.save_completed_result

        def crash_after_save(value):
            save(value)
            raise Crash()
        monkeypatch.setattr(pr, 'save_completed_result', crash_after_save)
    elif boundary == 'checkpoint':
        write = registry._write_checkpoint

        def crash_after_checkpoint():
            write()
            raise Crash()
        monkeypatch.setattr(registry, '_write_checkpoint', crash_after_checkpoint)
    else:
        put = registry.completion_queue.put
        def crash_on_enqueue(event):
            if boundary == 'queued':
                put(event)
            raise Crash()
        monkeypatch.setattr(registry.completion_queue, 'put', crash_on_enqueue)
    with pytest.raises(Crash):
        registry._finish_exited(session, 0)

    recovered = pr.ProcessRegistry()
    recovered.restore_completions()
    events = []
    while not recovered.completion_queue.empty():
        event = recovered.completion_queue.get_nowait()
        if event.get('type') == 'completion':
            events.append(event)
    assert len(events) == 1, 'durable result lost its pending completion notification'
    assert events[0]['output'] == 'actual final output'
    assert events[0]['parent_session_id'] == 'owner'
    record = json.loads((tmp_path / 'logs/process-results/proc_crash.json').read_text())
    assert record['notification']['status'] == 'pending'
    assert record['notification']['identity'] == ['completion', 'proc_crash', 1.0]
    assert recovered.restore_completions() == 0


def _finished(registry, sid, **fields):
    session = pr.ProcessSession(id=sid, command='build', started_at=1.0, session_key='owner',
                                parent_session_id='owner', notify_on_complete=True,
                                output_buffer='final', **fields)
    registry._running[sid] = session
    registry._finish_exited(session, 0)
    return session, registry.completion_queue.get_nowait()


def test_retention_preserves_pending_and_unknown_obligations(tmp_path, monkeypatch):
    from tools import process_registry_results as results
    from tools.process_registry_delivery import notification_state, settle_notification
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(results, 'MAX_RETAINED_RESULTS', 1)
    registry = pr.ProcessRegistry()
    _, pending = _finished(registry, 'proc_pending')
    session, unknown = _finished(registry, 'proc_unknown')
    settle_notification(unknown, 'unknown', reason='interrupted')
    pr.save_completed_result(session)  # late kill/result rewrite cannot reset disposition
    assert notification_state(unknown)['status'] == 'unknown'
    _, settled = _finished(registry, 'proc_settled')
    settle_notification(settled, 'terminal', reason='suppressed')
    # Every receipt is older than retention; unsettled obligations are not history.
    monkeypatch.setattr(results, 'RESULT_RETENTION_SECONDS', -1)
    assert {path.stem for path in results._result_paths()} == {'proc_pending', 'proc_unknown'}
    recovered = pr.ProcessRegistry()
    recovered.restore_completions()
    assert {recovered.completion_queue.get_nowait()['session_id'] for _ in range(2)} == {
        'proc_pending', 'proc_unknown'}
    assert notification_state(pending)['status'] == 'pending'


def test_reading_completed_output_durably_suppresses_replay(tmp_path, monkeypatch):
    from tools.process_registry_delivery import notification_state
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    session, event = _finished(registry, 'proc_consumed')
    assert registry.read_log(session.id)['output'] == 'final'
    assert notification_state(event)['status'] == 'terminal'
    recovered = pr.ProcessRegistry()
    recovered.restore_completions()
    assert recovered.completion_queue.empty()


def test_legacy_handoff_stays_unknown_instead_of_replaying_on_restart(tmp_path, monkeypatch):
    from tools.process_registry_delivery import notification_state
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    _, event = _finished(registry, 'proc_legacy')
    registry.completion_queue.put(event)
    assert len(registry.drain_notifications(session_key='owner')) == 1
    assert notification_state(event)['status'] == 'unknown'
    recovered = pr.ProcessRegistry()
    recovered.restore_completions()
    assert recovered.completion_queue.empty(), 'no FIFO can ever reconcile a legacy handoff'
    assert recovered.drain_notifications(session_key='owner') == []
    assert notification_state(event)['status'] == 'unknown'
    from tools import process_registry_results as results
    monkeypatch.setattr(results, 'RESULT_RETENTION_SECONDS', -1)
    assert results._result_paths() == [], 'uncertain legacy history must have bounded retention'


def test_foreign_profile_and_owner_cannot_consume_recovered_result(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.process_registry_delivery import notification_state
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    _, event = _finished(registry, 'proc_isolated')
    recovered = pr.ProcessRegistry()
    recovered.restore_completions()
    assert recovered.drain_notifications(session_key='other-owner') == []
    assert recovered.completion_queue.qsize() == 1
    token = set_hermes_home_override(tmp_path / 'foreign')
    try:
        assert recovered.drain_notifications(session_key='owner') == []
        assert recovered.completion_queue.qsize() == 1
    finally:
        reset_hermes_home_override(token)
    assert notification_state(event)['status'] == 'pending'
    assert len(recovered.drain_notifications(session_key='owner')) == 1


def test_old_ambiguous_result_is_not_replayed(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    directory = tmp_path / 'logs/process-results'
    directory.mkdir(parents=True)
    (directory / 'proc_old.json').write_text(json.dumps({
        'id': 'proc_old', 'started_at': 1.0, 'notify_on_complete': True, 'output': 'old'}))
    recovered = pr.ProcessRegistry()
    recovered.restore_completions()
    assert recovered.completion_queue.empty()


def test_saved_result_fences_leftover_running_checkpoint_before_pid_probe(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    registry = pr.ProcessRegistry()
    _finished(registry, 'proc_saved')
    (tmp_path / 'processes.json').write_text(json.dumps([{
        'session_id': 'proc_saved', 'pid': 123, 'started_at': 1.0,
        'session_key': 'owner', 'parent_session_id': 'owner', 'notify_on_complete': True}]))
    recovered = pr.ProcessRegistry()
    def must_not_probe(*_args):
        pytest.fail('a completed producer was re-adopted as a running PID')
    monkeypatch.setattr(recovered, '_detached_host_fate', must_not_probe)
    assert recovered.recover_from_checkpoint() == 0
    recovered.restore_completions()
    assert recovered.completion_queue.qsize() == 1
    assert json.loads((tmp_path / 'processes.json').read_text()) == []
