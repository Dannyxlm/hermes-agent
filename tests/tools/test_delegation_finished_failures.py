"""Normally returned failed children retain the runner's finalized result, not its early copy."""
import queue
import threading
import time
from types import SimpleNamespace

import pytest

import tools.delegate_tool as delegate
from tools import async_delegation as ad
from tools.delegate_tool_dispatch import _Batch, _execute_and_aggregate
from tools.process_registry import process_registry


class Child:
    def __init__(self, sid, result):
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.session_id, self.model, self.result = sid, "synthetic-model", result

    def run_conversation(self, **kwargs):
        return dict(self.result)

    def get_activity_summary(self):
        return {"api_call_count": 1, "current_tool": None, "last_activity_ts": 1000.0, "max_iterations": 50}

    def hard_interrupt(self, *args, **kwargs):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("status,count", [("failed", 1), ("failed", 2), ("interrupted", 1), ("timeout", 1)])
def test_returned_failure_keeps_transcript_trim_and_public_fields(monkeypatch, status, count):
    ad._reset_for_tests()
    monkeypatch.setattr(delegate, "_get_child_timeout", lambda: None)
    monkeypatch.setattr(delegate, "_get_worktree_isolation", lambda: False)
    offered = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", offered)
    partial = "partial finding. " * 3600
    result = {"final_response": "401 Unauthorized", "failed": True, "error": "401 Unauthorized", "api_calls": 1, "messages": []}
    if status == "interrupted":
        result = {"final_response": "Operation interrupted.", "interrupted": True, "api_calls": 3,
                  "messages": [{"role": "assistant", "content": partial}]}
    tasks = [{"goal": f"goal {i}"} for i in range(count)]
    paths = [f"/synthetic/live/task-{i}.jsonl" for i in range(count)]
    parent = SimpleNamespace(session_id="parent", _current_task_id=None, _active_children=[],
        _active_children_lock=threading.Lock(), _touch_activity=lambda value: None, _interrupt_requested=False)
    batch = _Batch(task_list=tasks, children=[(i, task, Child(f"c{i}", result)) for i, task in enumerate(tasks)],
        parent_agent=parent, creds={"model": "test"}, context=None, top_role="leaf", max_children=count,
        live_deleg_id=None, live_writers=[SimpleNamespace(path=p, finalize=lambda entry: None) for p in paths],
        live_paths=paths, origin_wake_sid="", origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False, overall_start=time.monotonic(),
        unit_id="finished-failure")
    if status == "timeout":
        # Inject the child timeout outcome; dispatch/record/finalization remain real.
        monkeypatch.setattr(_Batch, "run_child", lambda self, i, task, child: {
            "task_index": i, "status": "timeout", "summary": None, "error": "synthetic timeout",
            "_child_role": "leaf", "_child_cost_usd": 0.1})
    try:
        handle = ad.dispatch_async_delegation_batch(goals=[t["goal"] for t in tasks], context=None,
            toolsets=None, role="leaf", model="test", session_key="owner", delegation_id=batch.unit_id,
            runner=lambda: _execute_and_aggregate(batch, honor_parent_interrupt=False))
        assert handle["status"] == "dispatched"
        event = offered.get(timeout=10)
        while event.get("task_failure_notice"):
            event = offered.get(timeout=10)
        assert event["status"] == "error"
        durable = ad.get_durable_delegation(batch.unit_id)["result"]["results"]
        for entries in (event["results"], durable):
            assert len(entries) == count
            for i, entry in enumerate(entries):
                assert entry.get("live_transcript") == paths[i]
                assert not any(key.startswith("_child_") for key in entry)
                if status == "interrupted":
                    assert entry["summary_truncated"] is True
                    assert len(entry["summary"]) < len(partial)
                if status == "timeout":
                    assert entry["status"] == "timeout"
    finally:
        ad._reset_for_tests()
