"""Real result files and production FIFO admission/recovery; only inference is replaced."""
import asyncio
import queue

import pytest

from gateway.session_authorities import owner_scope
from tests.gateway import test_local_producer_admission as producer_tests
from tools.process_registry import ProcessRegistry, ProcessSession
from tools.process_registry_delivery import notification_state, restore_process_completions, settle_notification


gateway = producer_tests.gateway


class Crash(BaseException):
    pass


def finish(gateway, authority=None, sid=None, process='proc_durable', **fields):
    authority = authority or gateway.primary
    sid = sid or gateway.launch.session_id
    event = gateway.process_event(authority, sid, 'completion', process=process)
    registry = ProcessRegistry()
    session = ProcessSession(id=process, command='build', started_at=1.0,
                             session_key=event['session_key'], parent_session_id=event['parent_session_id'],
                             owner_task_id=fields.pop("owner_task_id", sid),
                             notify_on_complete=True, output_buffer='verified final output',
                             **fields)
    with owner_scope(authority):
        registry._running[session.id] = session
        registry._finish_exited(session, 0)
    return registry.completion_queue.get_nowait()


def recover(authority):
    offered = queue.Queue()
    with owner_scope(authority):
        restore_process_completions(offered)
    return offered


def state(authority, event):
    with owner_scope(authority):
        return notification_state(event)


@pytest.mark.asyncio
async def test_recovered_result_is_delivered_once_through_existing_fifo(gateway):
    await gateway.watch(gateway.primary, gateway.launch.session_id)
    event = finish(gateway)
    for _ in range(2):
        await gateway.runner._drain_watch_notifications(recover(gateway.primary))
    sid = gateway.launch.session_id
    await gateway.settled(gateway.primary, sid, 1)
    [row] = gateway.rows(gateway.primary, sid)
    assert row['principal_id'].startswith('automation:')
    assert row['outcome'] == 'completed'
    assert len(gateway.turns) == 1
    assert 'verified final output' in gateway.turns[0].text
    assert state(gateway.primary, event)['admission_id'] == row['admission_id']
    assert state(gateway.primary, event)['status'] == 'admitted'
    assert recover(gateway.primary).empty()


@pytest.mark.asyncio
async def test_admission_before_ack_crash_reconciles_exact_receipt_twice(gateway, monkeypatch):
    import gateway.process_completion_delivery as delivery
    await gateway.watch(gateway.primary, gateway.launch.session_id)
    event = finish(gateway)

    def crash(*_args):
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(delivery, 'acknowledge_process_admission', crash)
        with pytest.raises(Crash):
            await gateway.runner._deliver_completion_notification('[finished]', event)
    assert state(gateway.primary, event)['status'] == 'unknown'
    await gateway.settled(gateway.primary, gateway.launch.session_id, 1)
    # Replace the authority over the same temporary stores, dropping its in-memory dedup cache.
    await gateway.stop()
    await gateway.start()
    for _ in range(2):
        await gateway.runner._drain_watch_notifications(recover(gateway.primary))
    assert state(gateway.primary, event)['status'] == 'admitted'
    assert len(gateway.rows(gateway.primary, gateway.launch.session_id)) == 1
    assert len(gateway.turns) == 1


@pytest.mark.asyncio
async def test_unknown_without_receipt_never_blindly_resends(gateway):
    event = finish(gateway)
    with owner_scope(gateway.primary):
        settle_notification(event, 'unknown', reason='interrupted_admission')
    for _ in range(2):
        offered = recover(gateway.primary)
        await gateway.runner._drain_watch_notifications(offered)
        assert offered.qsize() == 1
    assert state(gateway.primary, event)['status'] == 'unknown'
    assert gateway.rows(gateway.primary, gateway.launch.session_id) == []
    assert gateway.turns == []


@pytest.mark.asyncio
async def test_busy_target_queues_behind_human_and_runs_once(gateway):
    from tests.waiting import async_wait_until
    sid, authority = gateway.launch.session_id, gateway.primary
    viewer = await gateway.watch(authority, sid)
    gateway.gate = asyncio.Event()
    reply = await viewer.dispatch({'id': 'prompt', 'method': 'prompt.submit', 'params': {
        'session_id': sid, 'input_id': 'human', 'text': 'first human'}})
    assert 'result' in reply
    await async_wait_until(lambda: gateway.turns, timeout=30, what='human turn')
    event = finish(gateway)
    await gateway.runner._drain_watch_notifications(recover(authority))
    rows = gateway.rows(authority, sid)
    assert [row['status'] for row in rows] == ['started', 'queued']
    assert state(authority, event)['status'] == 'admitted'
    gateway.gate.set()
    await gateway.settled(authority, sid, 2)
    assert gateway.turns[0].text == 'first human'
    assert 'verified final output' in gateway.turns[1].text


@pytest.mark.asyncio
async def test_permanently_gone_is_terminal_not_replayed(gateway):
    event = finish(gateway)
    assert gateway.primary.db.delete_session(event['parent_session_id'])
    await gateway.runner._drain_watch_notifications(recover(gateway.primary))
    assert state(gateway.primary, event)['reason'] == 'target_gone'
    assert state(gateway.primary, event)['status'] == 'terminal'
    assert recover(gateway.primary).empty()
    assert gateway.turns == []


@pytest.mark.asyncio
async def test_suppressed_child_result_is_terminal_not_replayed(gateway):
    child = finish(gateway, process='proc_child', owner_task_id='sa-child')
    await gateway.runner._drain_watch_notifications(recover(gateway.primary))
    assert state(gateway.primary, child)['status'] == 'terminal'
    assert recover(gateway.primary).empty()
    assert gateway.turns == []


@pytest.mark.asyncio
async def test_secondary_profile_recovery_never_enters_launch_fifo(gateway):
    authority = gateway.secondary
    local = gateway.create(authority, gateway.connect(authority), 'secondary', 'gui')
    await gateway.watch(authority, local.session_id)
    event = finish(gateway, authority, local.session_id)
    assert recover(gateway.primary).empty()
    offered = recover(authority)
    assert offered.qsize() == 1
    await gateway.runner._drain_watch_notifications(offered)
    await gateway.settled(authority, local.session_id, 1)
    assert gateway.rows(gateway.primary, gateway.launch.session_id) == []
    assert [turn.profile for turn in gateway.turns] == ['beta']
    assert state(authority, event)['status'] == 'admitted'
    with owner_scope(gateway.primary), pytest.raises(ValueError, match='profile mismatch'):
        settle_notification(event, 'terminal', reason='foreign')


@pytest.mark.asyncio
async def test_transient_admission_refusal_remains_pending_then_delivers(gateway, monkeypatch):
    from gateway.drain_control import open_restart_wait
    sid = gateway.launch.session_id
    await gateway.watch(gateway.primary, sid)
    event = finish(gateway)
    gateway.runner._running = gateway.runner._draining = True
    open_restart_wait(gateway.runner)
    offered = recover(gateway.primary)
    await gateway.runner._drain_watch_notifications(offered)
    assert state(gateway.primary, event)['status'] == 'pending'
    assert offered.qsize() == 1
    assert gateway.rows(gateway.primary, sid) == []
    gateway.runner._draining = False
    import gateway.process_completion_delivery as delivery
    from types import SimpleNamespace
    next_due = offered.queue[0]['_process_next_retry_at']
    monkeypatch.setattr(delivery, 'time', SimpleNamespace(monotonic=lambda: next_due + 1))
    await gateway.runner._drain_watch_notifications(offered)
    await gateway.settled(gateway.primary, sid, 1)
    assert state(gateway.primary, event)['status'] == 'admitted'
    assert len(gateway.turns) == 1


@pytest.mark.asyncio
async def test_coalesced_members_reconcile_one_fifo_receipt_after_ack_crash(gateway, monkeypatch):
    import gateway.process_completion_delivery as delivery
    from gateway.session_automation import producer_identity
    sid = gateway.launch.session_id
    await gateway.watch(gateway.primary, sid)
    events = [finish(gateway, process=process) for process in ('proc_batch_a', 'proc_batch_b')]
    primary = {**events[0], '_automation_identities': [producer_identity(gateway.runner, e) for e in events]}
    def crash(*_args):
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(delivery, 'acknowledge_process_admission', crash)
        with pytest.raises(Crash):
            await gateway.runner._deliver_completion_notification('[two processes finished]', primary)
    assert [state(gateway.primary, e)['status'] for e in events] == ['unknown', 'unknown']
    await gateway.settled(gateway.primary, sid, 1)
    for _ in range(2):
        await gateway.runner._drain_watch_notifications(recover(gateway.primary))
    assert [state(gateway.primary, e)['status'] for e in events] == ['admitted', 'admitted']
    assert len({state(gateway.primary, e)['admission_id'] for e in events}) == 1
    assert len(gateway.rows(gateway.primary, sid)) == len(gateway.turns) == 1
