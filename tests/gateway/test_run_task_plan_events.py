import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.platforms import api_server_runs


@pytest.mark.asyncio
async def test_completed_todo_result_emits_full_revisioned_snapshot_and_clear():
    queue = asyncio.Queue()
    adapter = SimpleNamespace(_run_statuses={"run": {"status": "running"}},
                              _run_streams={"run": queue}, _set_run_status=Mock())
    callback = api_server_runs._make_run_event_callback(
        adapter, "run", asyncio.get_running_loop(),
        _api_server=SimpleNamespace(redact_sensitive_text=lambda text, **kw: text))
    todos = [{"id": "parent", "content": "Build the feature", "status": "in_progress"},
             {"id": "child", "content": "Inspect the complete result", "status": "completed", "parent": "parent"}]
    for revision, steps in [(2, todos), (3, [])]:
        callback("tool.completed", "todo_list", result=json.dumps({"todos": steps, "revision": revision}))
        await asyncio.sleep(0)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        plans = [event for event in events if event["event"] == "todo.updated"]
        assert len(plans) == 1
        assert plans[0]["todos"] == steps and plans[0]["revision"] == revision
        assert plans[0]["run_id"] == "run"


@pytest.mark.asyncio
async def test_non_todo_and_failed_results_do_not_publish_plan_and_plan_text_is_redacted():
    queue = asyncio.Queue()
    redact = Mock(side_effect=lambda text, **kw: text.replace("fake-secret", "[redacted]"))
    adapter = SimpleNamespace(_run_statuses={}, _run_streams={"run": queue}, _set_run_status=Mock())
    callback = api_server_runs._make_run_event_callback(
        adapter, "run", asyncio.get_running_loop(), _api_server=SimpleNamespace(redact_sensitive_text=redact))
    raw = {"todos": [{"id": "one", "content": "fake-secret", "status": "pending"}], "revision": 1}
    callback("tool.completed", "terminal", result=json.dumps(raw))
    callback("tool.completed", "todo_list", result=json.dumps(raw), is_error=True)
    await asyncio.sleep(0)
    while not queue.empty():
        assert queue.get_nowait()["event"] != "todo.updated"
    callback("tool.completed", "todo", result=json.dumps(raw))
    await asyncio.sleep(0)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    plan = next(event for event in events if event["event"] == "todo.updated")
    assert plan["todos"][0]["content"] == "[redacted]"
    assert redact.call_args.kwargs["force"] is True
