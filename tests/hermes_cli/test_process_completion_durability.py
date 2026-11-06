"""Legacy CLI queue handoff delivers output without inventing a durable consumption receipt."""
import queue

from cli import HermesCLI
from tools import process_registry as pr
from tools.process_registry_delivery import notification_state, restore_process_completions
from tools.process_registry_notifications import ProcessNotificationBatch


def test_legacy_cli_receives_result_once_and_keeps_uncertain_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    process = pr.ProcessSession(id='proc_cli', command='build', started_at=1,
        session_key='owner', parent_session_id='owner', notify_on_complete=True,
        output_buffer='CLI final output')
    registry._running[process.id] = process
    registry._finish_exited(process, 0)
    with registry.completion_queue.mutex:
        event = dict(registry.completion_queue.queue[0])
    monkeypatch.setattr(pr, 'process_registry', registry)
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = 'owner'
    cli._session_db = None
    cli._pending_input = queue.Queue()
    monkeypatch.setattr(cli, '_background_notifications_suppressed', lambda: False)
    cli._drain_process_notifications('cli-test')
    pending = cli._pending_input.get_nowait()
    assert isinstance(pending, ProcessNotificationBatch)
    assert 'CLI final output' in pending.render(registry)
    assert not registry.is_completion_consumed(process.id)
    receipt = notification_state(event)
    assert receipt['status'] == 'unknown'
    assert receipt['admission_id'] is None
    assert receipt['reason'] == 'legacy_consumer_handoff'
    cli._drain_process_notifications('cli-test')
    assert cli._pending_input.empty()
    restored = queue.Queue()
    for _ in range(3):
        assert restore_process_completions(restored) == 0
    assert restored.empty()
