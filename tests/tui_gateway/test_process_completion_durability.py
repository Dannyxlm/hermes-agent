"""Real process receipts must survive refused legacy TUI submissions without false ACKs."""
import queue
import threading
from types import SimpleNamespace

import pytest

from tools import process_registry as pr
from tools.process_registry_delivery import notification_state
from tools.process_registry_notifications import format_process_notification
from tui_gateway import server


@pytest.mark.parametrize('batch', [False, True])
def test_durable_completion_refused_then_accepted(monkeypatch, tmp_path, batch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(id='proc_retry', command='build', started_at=1,
                                session_key='owner', parent_session_id='owner',
                                notify_on_complete=True, output_buffer='actual final output')
    registry._running[session.id] = session
    registry._finish_exited(session, 0)
    event = registry.completion_queue.get_nowait()
    monkeypatch.setattr(pr, 'process_registry', registry)
    monkeypatch.setattr(server, '_emit', lambda *a: None)
    monkeypatch.setattr(server, '_background_notifications_off', lambda s: False)
    monkeypatch.setattr(server, 'time', SimpleNamespace(time=lambda: 1, sleep=lambda _: None))
    accepted = []
    decisions = iter([False, True])

    def submit(*args, **kwargs):
        # Exercise the real _notif_submit, including its release on refusal.
        if next(decisions):
            accepted.append((args, kwargs))
            return True
        return False

    monkeypatch.setattr(server, '_run_prompt_submit', submit)
    ui = {'history_lock': threading.RLock(), 'session_key': 'owner', 'running': False}
    emitted = set()

    def dispatch(evt):
        completions = [] if batch else None
        server._notif_handle_event('ui', ui, evt, emitted, registry,
                                   format_process_notification, None, completions, owned=True)
        if batch:
            server._notif_dispatch_completions('ui', ui, completions, registry, None)

    dispatch(event)
    assert ui['running'] is False
    assert notification_state(event)['status'] == 'pending'
    dispatch(registry.completion_queue.get_nowait())
    assert len(accepted) == 1
    assert 'actual final output' in accepted[0][0][3]
    assert notification_state(event)['status'] == 'terminal'
    assert notification_state(event)['reason'] == 'legacy_submit_accepted'
    assert registry.completion_queue.empty()
    replay = pr.ProcessRegistry()
    replay.restore_completions()
    assert replay.completion_queue.empty()


@pytest.mark.parametrize('batch', [False, True])
def test_ambiguous_submit_is_not_replayed_or_claimed_consumed(monkeypatch, tmp_path, batch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    process = pr.ProcessSession(id='proc_uncertain', command='build', started_at=1,
                                notify_on_complete=True, output_buffer='result')
    registry._running[process.id] = process
    registry._finish_exited(process, 0)
    event = registry.completion_queue.get_nowait()
    ui = {'history_lock': threading.RLock(), 'running': not batch}

    def ambiguous(*args, **kwargs):
        raise OSError('submit outcome unknown')

    monkeypatch.setattr(server, '_run_prompt_submit', ambiguous)
    if batch:
        server._notif_dispatch_completions('ui', ui, [(event, 'result')], registry, None)
    else:
        server._notif_dispatch_event('ui', ui, event, 'result')
    assert ui['running'] is False
    assert notification_state(event)['status'] == 'unknown'
    assert notification_state(event)['admission_id'] is None
    replay = pr.ProcessRegistry()
    replay.restore_completions()
    assert replay.completion_queue.empty()
