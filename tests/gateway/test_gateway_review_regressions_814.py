"""Focused regressions for completion batching and concise exit status."""

import asyncio
from collections import OrderedDict
from threading import Lock
from unittest.mock import AsyncMock

from gateway.run import GatewayRunner, _format_concise_process_notification


def _completion_event(*, parent_session_id: str, session_id: str, output: str) -> dict:
    return {
        "type": "completion",
        "session_id": session_id,
        "session_key": "agent:main:telegram:dm:123",
        "parent_session_id": parent_session_id,
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "started_at": 1234.5,
        "command": "echo done",
        "exit_code": 0,
        "completion_reason": "exited",
        "output": output,
    }


def _batching_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._completion_delivery_lock = Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    runner._completion_notification_batch_window = 0
    runner._background_tasks = set()
    return runner


def test_completion_batches_do_not_cross_parent_session_boundaries():
    """An old session's output must not ride the live session's delivery."""
    runner = _batching_runner()
    runner._deliver_completion_notification = AsyncMock(return_value=True)
    old_event = _completion_event(
        parent_session_id="session-before-reset",
        session_id="proc-old",
        output="OLD SESSION OUTPUT",
    )
    live_event = _completion_event(
        parent_session_id="session-after-reset",
        session_id="proc-live",
        output="LIVE SESSION OUTPUT",
    )

    async def _exercise():
        return await asyncio.gather(
            runner._enqueue_process_completion_notification(
                "old delivery\nOLD SESSION OUTPUT", old_event,
            ),
            runner._enqueue_process_completion_notification(
                "live delivery\nLIVE SESSION OUTPUT", live_event,
            ),
        )

    assert asyncio.run(_exercise()) == [True, True]
    assert runner._deliver_completion_notification.await_count == 2
    deliveries = {
        call.args[1]["parent_session_id"]: call.args[0]
        for call in runner._deliver_completion_notification.await_args_list
    }
    assert deliveries == {
        "session-before-reset": "old delivery\nOLD SESSION OUTPUT",
        "session-after-reset": "live delivery\nLIVE SESSION OUTPUT",
    }
    assert "OLD SESSION OUTPUT" not in deliveries["session-after-reset"]


def test_concise_unknown_exit_status_is_neutral_not_success():
    text = _format_concise_process_notification(
        "proc-recovered", "detached-command", None, "possibly partial output",
    )

    assert text.startswith("⚪ Background task ended (exit status unknown)")
    assert "✅" not in text
    assert "❌" not in text
    assert "possibly partial output" not in text
