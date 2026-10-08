"""Notifier polling has an independent gateway config gate."""

import asyncio
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner(with_adapter=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: MagicMock()} if with_adapter else {}
    runner._kanban_sub_fail_counts = {}
    return runner


def test_notifier_watcher_skips_when_notifications_disabled():
    runner = _make_runner(with_adapter=True)

    with patch(
        "hermes_cli.config.load_config",
        return_value={"kanban": {"notify_in_gateway": False}},
    ):
        with patch("hermes_cli.kanban_db.list_boards") as list_boards:
            asyncio.run(runner._kanban_notifier_watcher())

    list_boards.assert_not_called()


def test_notifier_watcher_polls_without_dispatch_ownership():
    """A profile gateway still polls its profile-owned subscriptions."""
    runner = _make_runner(with_adapter=True)
    past_gate = []
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        # Stop after the initial delay + first per-interval sleep so the loop
        # body runs exactly once.
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "kanban": {
                "dispatch_in_gateway": False,
                "notify_in_gateway": True,
            }
        },
    ):
        with patch.object(
            _kb, "list_boards",
            side_effect=lambda *a, **kw: past_gate.append(True) or [],
        ):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                with patch("asyncio.to_thread", side_effect=fake_to_thread):
                    asyncio.run(runner._kanban_notifier_watcher())

    assert past_gate, (
        "gateways without the dispatch lock must still poll owned subscriptions"
    )


def _run_dispatcher_ticks(results_per_tick, caplog):
    """Drive the embedded dispatcher loop over canned tick results; return the
    "stuck" warnings it logged. No board, process or sleep is real."""
    import logging
    from types import SimpleNamespace
    import gateway.kanban_watchers as kw

    runner = _make_runner()
    ticks = iter(results_per_tick)

    class FakeDispatcher:
        def __init__(self, *_args):
            pass

        def tick_once(self):
            return next(ticks)

        def ready_nonempty(self):
            return True

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def no_sleep(_delay):
        return None

    remaining = [len(results_per_tick)]

    async def between_ticks(_interval):
        remaining[0] -= 1
        if remaining[0] <= 0:
            runner._running = False

    runner._sleep_between_ticks = between_ticks
    boot = (lambda: {}, MagicMock(), {})
    caplog.set_level(logging.WARNING, logger=kw.logger.name)
    with patch.object(GatewayRunner, "_kanban_dispatcher_boot", return_value=boot), \
            patch.object(kw, "_KanbanDispatcher", FakeDispatcher), \
            patch.object(kw, "_resolve_dispatcher_settings", return_value=SimpleNamespace(interval=1.0)), \
            patch.object(kw, "_kanban_dispatch_allowed", return_value=True), \
            patch.object(kw, "_resolve_auto_decompose_settings", return_value=(False, 0)), \
            patch.object(kw, "_to_thread_process_service", side_effect=fake_to_thread), \
            patch("hermes_cli.kanban_db_dispatch.reap_worker_zombies", return_value=[]), \
            patch("asyncio.sleep", side_effect=no_sleep):
        asyncio.run(runner._kanban_dispatcher_watcher())
    return [r.getMessage() for r in caplog.records if "dispatcher stuck" in r.getMessage()]


def test_dispatcher_cap_hold_is_not_reported_stuck(caplog):
    """Round 9 U30 (R25): ready cards held only by max_in_progress_per_profile are busy, not stuck."""
    from hermes_cli.kanban_db_dispatch import DispatchResult
    capped = DispatchResult(skipped_per_profile_capped=[("t_a", "default", 1)])
    assert _run_dispatcher_ticks([[("r9", capped)]] * 8, caplog) == []


def test_dispatcher_unexplained_ready_card_still_reported_stuck(caplog):
    from hermes_cli.kanban_db_dispatch import DispatchResult
    mixed = DispatchResult(skipped_per_profile_capped=[("t_a", "default", 1)],
                           skipped_unassigned=["t_b"], ready_unexplained=1)
    warnings = _run_dispatcher_ticks([[("r9", mixed)]] * 8, caplog)
    assert len(warnings) == 1
    assert "profile_cap=1" in warnings[0]
