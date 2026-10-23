"""Provider tool invocation identity is host-supplied in both dispatch paths."""
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import InlineToolContext, resolve_invoke_tool_executor
from agent.memory_manager import MemoryManager
from agent.tool_executor import _ToolCallRef, _resolve_sequential_dispatch


@pytest.mark.parametrize("concurrent", [False, True])
def test_memory_tool_receives_host_invocation_not_a_model_argument(monkeypatch, concurrent):
    seen = []
    provider = SimpleNamespace(name="fixture", handle_tool_call=lambda name, args, **kw: seen.append((name, args, kw)) or "saved")
    manager = MemoryManager()
    manager._tool_to_provider["fixture_save"] = provider
    agent = SimpleNamespace(_memory_manager=manager, _memory_provider_tool_names={"fixture_save"},
                            _context_engine_tool_names=set(), _should_emit_quiet_tool_messages=lambda: False)
    args = {"content": "intent", "tool_call_id": "model-cannot-spoof-this"}
    if concurrent:
        executor = resolve_invoke_tool_executor(agent, "fixture_save")
        result = executor(agent, args, InlineToolContext(effective_task_id="task", tool_call_id="durable-host-call"))
    else:
        ref = _ToolCallRef("fixture_save", args, "task", "durable-host-call", [])
        result = _resolve_sequential_dispatch(agent, ref, []).execute(args)
    assert result == "saved"
    assert seen[0][2] == {"tool_call_id": "durable-host-call"}


def test_legacy_provider_without_kwargs_still_dispatches():
    manager = MemoryManager()
    provider = SimpleNamespace(name="legacy", handle_tool_call=lambda name, args: "legacy-result")
    manager._tool_to_provider["legacy_tool"] = provider
    assert manager.handle_tool_call("legacy_tool", {}, tool_call_id="host-call") == "legacy-result"
