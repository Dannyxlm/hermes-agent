"""A poisoned recovered process must not starve healthy completion, watch or heartbeat inputs."""
import queue

import pytest

from tests.gateway import test_process_completion_durability as durability

finish, gateway, recover, state = durability.finish, durability.gateway, durability.recover, durability.state


@pytest.mark.asyncio
@pytest.mark.parametrize('poison', ['missing_receipt', 'foreign_home'])
async def test_poisoned_recovery_is_isolated_and_backed_off(gateway, monkeypatch, poison):
    import gateway.process_completion_delivery as delivery
    sid, authority = gateway.launch.session_id, gateway.primary
    await gateway.watch(authority, sid)
    healthy = finish(gateway)
    bad = dict(healthy, session_id='proc_missing') if poison == 'missing_receipt' else dict(
        healthy, _process_result_home=str(gateway.tmp_path / 'foreign'))
    bad['_process_completion_recovery'] = True
    waiting = recover(authority)
    events = [waiting.get_nowait() for _ in range(waiting.qsize())]
    waiting.put(bad)
    for event in events:
        waiting.put(event)
    waiting.put(gateway.process_event(authority, sid, 'watch_match', process='proc_watch',
        event_id='watch-1', pattern='ready', output='watch ready'))
    waiting.put(gateway.tick(authority, sid, 1, process='proc_beat'))
    now = [100.0]
    monkeypatch.setattr(delivery, 'time', type('Clock', (), {'monotonic': staticmethod(lambda: now[0])}), raising=False)
    await gateway.runner._drain_watch_notifications(waiting)
    await gateway.settled(authority, sid, 3)
    assert state(authority, healthy)['status'] == 'admitted'
    assert len(gateway.turns) == 3
    assert any('watch ready' in turn.text for turn in gateway.turns)
    assert any('heartbeat #1' in turn.text for turn in gateway.turns)
    [retained] = [waiting.get_nowait() for _ in range(waiting.qsize())]
    assert retained['session_id'] == bad['session_id']
    assert retained['_process_next_retry_at'] > now[0]
    waiting.put(retained)
    # Before its due time even a permanently malformed event must not repeat disk/route probes.
    async def should_not_attempt(*a, **k):
        pytest.fail('poisoned completion retried without backoff')
    with monkeypatch.context() as patch:
        patch.setattr(gateway.runner, '_deliver_completion_notification', should_not_attempt)
        await gateway.runner._drain_watch_notifications(waiting)
    assert waiting.qsize() == 1
    now[0] = retained['_process_next_retry_at'] + 1
    await gateway.runner._drain_watch_notifications(waiting)
    assert waiting.qsize() == 1
    assert len(gateway.turns) == 3


@pytest.mark.asyncio
async def test_recovery_cancellation_preserves_snapshot(gateway, monkeypatch):
    import asyncio
    from gateway.process_completion_delivery import drain_recovered_process_completions
    events = [dict(finish(gateway, process=f'proc_cancel_{i}'), _process_completion_recovery=True)
              for i in range(2)]
    waiting = queue.Queue()
    for event in events:
        waiting.put(event)
    async def cancelled(*a, **k):
        raise asyncio.CancelledError
    monkeypatch.setattr(gateway.runner, '_deliver_completion_notification', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await drain_recovered_process_completions(gateway.runner, waiting)
    assert [waiting.get_nowait() for _ in range(waiting.qsize())] == events
