"""A wake receipted before a restart drain must run exactly once on the authority route too.

Once a chat has a committed human turn, ``deliver_wake`` commits a Kanban wake to the session
authority's FIFO (``admit_route_wake``) instead of the classic adapter path the sibling file covers.
The receipt lets the notifier advance its cursor, so the wake is never re-sent: a drain must leave
the committed row queued (or run it), and the restarted gateway's startup recovery must run it, never
twice. Real GatewayRunner, DiscordAdapter and SessionAuthority over SQLite; the restart is a second
runner and authority over the same state DB.
"""

import asyncio
from contextlib import asynccontextmanager
import threading

import pytest

from tests.waiting import async_wait_until

WAKE = "[Kanban task completed] t_1"


class Hold:
    """Keeps the human turn running until ``finish`` is set."""

    def __init__(self):
        self.running, self.finish = asyncio.Event(), asyncio.Event()


@asynccontextmanager
async def authority_route(monkeypatch, instance_id, executed, hold=None):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture-token", typing_indicator=False))

    async def send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="sent")

    monkeypatch.setattr(adapter, "send", send)
    adapter.gateway_runner = runner  # as GatewayRunner._create_adapter binds every adapter
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id=instance_id)
    runner._wire_adapter_handlers(adapter)

    async def turn(event, source, quick_key, run_generation):
        executed.append((instance_id, event.text))
        if hold is not None and event.text == "A":
            hold.running.set()
            await hold.finish.wait()
        captured = execution_result.get()
        if captured is not None:
            captured["result"] = {"final_response": "reply " + event.text, "messages": []}
        return "reply " + event.text

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    # Only the authority/runtime lifecycle is under test; no listener or live transport is started.
    runner.session_runtime_descriptor = {"state": "ready", "capabilities": []}
    try:
        yield runner, adapter, authority, adapter.build_source(chat_id="42", chat_type="dm", user_id="42")
    finally:
        if hold is not None:
            hold.finish.set()
        await stop_route(runner, adapter, authority)
        from gateway.session_cron import unbind_owner
        unbind_owner(authority)
        assert runner._shutdown_executor(drain_timeout=10) == 0
        runner.session_store.close_all_db_handles()
        runner.close_all_session_db_handles()
        authority.db.close()


async def stop_route(runner, adapter, authority):
    from gateway.run_runtime import _authority_tasks, drain_gateway_runtime, settle_gateway_runtime
    from gateway.session_runtime_workers import mutation_tasks, physical_workers, side_agents

    await drain_gateway_runtime(runner)
    await asyncio.wait_for(settle_gateway_runtime(runner), 10)
    # A terminal SQLite row alone is NOT quiescence: delivery, publication, media collection,
    # the FIFO's next off-loop claim, and the adapter's human reply may still own the old epoch.
    while adapter._background_tasks:
        await asyncio.wait_for(asyncio.gather(*list(adapter._background_tasks)), 10)
    assert all(task.done() and not task.cancelled() and task.exception() is None
               for task in _authority_tasks(authority))
    assert not mutation_tasks(authority)
    assert not list(physical_workers(authority))
    assert not side_agents(authority)
    assert not authority.executing
    assert not authority.pending_results


def admission_rows(runner, authority, source):
    from hermes_state_runtime import list_session_admissions

    owner = runner.session_store.peek_session_id(runner.session_store._generate_session_key(source))
    return list_session_admissions(authority.db, session_id=owner, pending_only=False)


def admissions(runner, authority, source):
    rows = admission_rows(runner, authority, source)
    return [(row["principal_id"].split(":")[0], row["status"], row.get("outcome")) for row in rows]


def wake_runs(executed):
    return [owner for owner, text in executed if text == WAKE]


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_authority_route_runs_a_wake_receipted_before_drain_once_after_restart(monkeypatch, busy):
    from gateway import session_authority
    from gateway.platforms.event import MessageEvent
    from gateway.run_runtime import drain_gateway_runtime, recover_profile_native_sessions
    from gateway.wake import deliver_wake
    from hermes_state_runtime import get_session_admission

    executed, hold = [], Hold() if busy else None
    async with authority_route(monkeypatch, "owner", executed, hold) as (runner, adapter, authority, source):
        await adapter.handle_message(MessageEvent(text="A", source=source, message_id="A"))
        if hold is not None:
            await asyncio.wait_for(hold.running.wait(), 10)
        else:
            await async_wait_until(
                lambda: admissions(runner, authority, source) == [("messaging", "terminal", "completed")],
                what="the human turn to settle",
            )
        claiming, release = asyncio.Event(), threading.Event()
        loop, claim, epoch = asyncio.get_running_loop(), session_authority.claim_session_input, authority.epoch

        def before_claim(db, **kwargs):
            head = kwargs.get("head")
            row = get_session_admission(db, admission_id=head) if head else None
            if kwargs["epoch"] == epoch and row and row["principal_id"].startswith("automation:"):
                # The loop-side gate passed, but the real transaction (and its drain guard) has
                # not started. Merely awaiting deliver_wake cannot establish this ordering.
                loop.call_soon_threadsafe(claiming.set)
                assert release.wait(10)
            return claim(db, **kwargs)

        monkeypatch.setattr(session_authority, "claim_session_input", before_claim)
        try:
            await asyncio.wait_for(deliver_wake(adapter, text=WAKE, source=source), 10)  # receipt: no resend
            if hold is not None:
                hold.finish.set()
            await asyncio.wait_for(claiming.wait(), 10)
            wake = admission_rows(runner, authority, source)[-1]
            assert admissions(runner, authority, source)[-1] == ("automation", "queued", None)
            await drain_gateway_runtime(runner)
        finally:
            release.set()
        await stop_route(runner, adapter, authority)
        assert admissions(runner, authority, source) == [
            ("messaging", "terminal", "completed"), ("automation", "queued", None)]
        assert wake_runs(executed) == []

    # Bootstrap may advance the epoch only after the previous owner's tasks and writers ended.
    async with authority_route(monkeypatch, "successor", executed) as (successor, adapter, authority, source):
        assert authority.epoch != epoch
        assert await recover_profile_native_sessions(successor, authority) == {wake["target_session_id"]: "ready"}
        await async_wait_until(
            lambda: admissions(successor, authority, source)[-1] == ("automation", "terminal", "completed"),
            what="the wake to run",
        )
        await stop_route(successor, adapter, authority)
        rows = admission_rows(successor, authority, source)
        assert len(rows) == 2
        assert rows[-1]["admission_id"] == wake["admission_id"]
        assert rows[-1]["owner_epoch"] == authority.epoch
        assert wake_runs(executed) == ["successor"]
        assert await recover_profile_native_sessions(successor, authority) == {}
        assert wake_runs(executed) == ["successor"]  # recovery is idempotent; producer never resends


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claimed", "settling"])
async def test_claimed_wake_settles_before_restart_transfers_its_epoch(monkeypatch, stage):
    """A supported restart joins an already claimed turn, including its off-loop terminal write.

    This is deliberately separate from the receipt-before-claim contract: 'started' is valid
    here, but can never stand in for queued/completed in the preclaim test above.
    """
    from gateway import run_runtime, session_settlement_recovery
    from gateway.platforms.event import MessageEvent
    from gateway.wake import deliver_wake

    entered, release, settling = asyncio.Event(), asyncio.Event(), asyncio.Event()
    executed = []
    async with authority_route(monkeypatch, "owner", executed) as (runner, adapter, authority, source):
        await adapter.handle_message(MessageEvent(text="A", source=source, message_id="A"))
        await async_wait_until(
            lambda: admissions(runner, authority, source) == [("messaging", "terminal", "completed")],
            what="the human turn to settle",
        )
        execute, settle = authority._execute_claimed, session_settlement_recovery.settle_with_retry

        async def held_execute(ref, live, row):
            if stage == "claimed" and row["principal_id"].startswith("automation:"):
                entered.set()
                await release.wait()
            return await execute(ref, live, row)

        async def held_settle(owner, live, ref, row, *args):
            if owner is authority and stage == "settling" and row["principal_id"].startswith("automation:"):
                entered.set()
                await release.wait()
            return await settle(owner, live, ref, row, *args)

        tasks = run_runtime._authority_tasks

        def snapshot(owner):
            found = tasks(owner)
            if owner is authority:
                settling.set()  # real shutdown has snapshotted the still-live claim/settlement
            return found

        monkeypatch.setattr(authority, "_execute_claimed", held_execute)
        monkeypatch.setattr(session_settlement_recovery, "settle_with_retry", held_settle)
        monkeypatch.setattr(run_runtime, "_authority_tasks", snapshot)
        stopping = None
        try:
            await asyncio.wait_for(deliver_wake(adapter, text=WAKE, source=source), 10)  # one receipt, no resend
            await asyncio.wait_for(entered.wait(), 10)
            wake = admission_rows(runner, authority, source)[-1]
            assert (wake["status"], wake["outcome"], wake["owner_epoch"]) == ("started", None, authority.epoch)
            assert wake_runs(executed) == ([] if stage == "claimed" else ["owner"])
            await run_runtime.drain_gateway_runtime(runner)
            stopping = asyncio.create_task(run_runtime.settle_gateway_runtime(runner))
            await asyncio.wait_for(settling.wait(), 10)
            assert not stopping.done(), "restart released an unsettled claim"
            assert authority.db._read_one("SELECT epoch FROM runtime_epoch WHERE singleton=1")[0] == authority.epoch
            release.set()
            await asyncio.wait_for(stopping, 10)
            await stop_route(runner, adapter, authority)
            assert admissions(runner, authority, source)[-1] == ("automation", "terminal", "completed")
            settled = admission_rows(runner, authority, source)[-1]
            assert wake_runs(executed) == ["owner"]
        finally:
            release.set()
            if stopping is not None:
                await asyncio.wait_for(stopping, 10)

    async with authority_route(monkeypatch, "successor", executed) as (successor, adapter, authority, source):
        assert authority.epoch != settled["owner_epoch"]
        assert await run_runtime.recover_profile_native_sessions(successor, authority) == {}
        assert admission_rows(successor, authority, source)[-1] == settled
        assert len(admission_rows(successor, authority, source)) == 2
        assert wake_runs(executed) == ["owner"]


@pytest.mark.asyncio
async def test_a_wake_claimed_just_before_a_drain_runs_rather_than_settling_unrun(monkeypatch):
    """A drain after claim commit must run the wake, not settle it with the drain notice."""
    from gateway.platforms.event import MessageEvent
    from gateway.run_runtime import settle_gateway_runtime
    from gateway.wake import deliver_wake

    executed = []
    async with authority_route(monkeypatch, "owner", executed) as (runner, adapter, authority, source):
        await adapter.handle_message(MessageEvent(text="A", source=source, message_id="A"))
        await async_wait_until(
            lambda: admissions(runner, authority, source) == [("messaging", "terminal", "completed")],
            what="the human turn to settle",
        )
        stamp = authority._stamp_claim

        def drain_begins_after_commit(ref, live, row):
            if row:
                runner._draining = True
            return stamp(ref, live, row)

        monkeypatch.setattr(authority, "_stamp_claim", drain_begins_after_commit)
        await asyncio.wait_for(deliver_wake(adapter, text=WAKE, source=source), 10)
        await settle_gateway_runtime(runner)
        assert admissions(runner, authority, source)[-1] == ("automation", "terminal", "completed")
        assert wake_runs(executed) == ["owner"]
