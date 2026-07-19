from __future__ import annotations

import json
from types import SimpleNamespace

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class RecordingProvider(MemoryProvider):
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    @property
    def name(self) -> str:
        return "recording"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self.calls.append(("initialize", session_id))

    def get_tool_schemas(self):
        return [
            {
                "name": "memory_read",
                "description": "Read existing memory.",
                "parameters": {"type": "object", "properties": {}},
            }
        ]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        self.calls.append(("tool", tool_name, args))
        if self.fail:
            raise RuntimeError("secret provider response fragment")
        return json.dumps(
            {
                "status": "ok",
                "data_class": "tainted_provider_data",
                "instruction_policy": "data_only_non_authoritative",
                "data": {"value": "reference only"},
            }
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.calls.append(("prefetch", query))
        return "legacy context"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self.calls.append(("queue_prefetch", query))

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs) -> None:
        self.calls.append(("sync", user_content, assistant_content))

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self.calls.append(("turn", turn_number))

    def on_session_end(self, messages) -> None:
        self.calls.append(("session_end", len(messages)))

    def on_pre_compress(self, messages) -> str:
        self.calls.append(("compress", len(messages)))
        return "legacy compression"


class StrictRuntime:
    active = True
    provider_name = "recording"

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.authorizations = []

    def authorize_explicit_read(self, **kwargs):
        self.authorizations.append(kwargs)
        return SimpleNamespace(
            allowed=self.allowed,
            code="memory_read_allowed" if self.allowed else "memory_subject_denied",
        )


def test_strict_manager_allows_only_explicit_authorized_tool_reads() -> None:
    runtime = StrictRuntime()
    provider = RecordingProvider()
    manager = MemoryManager(memory_runtime=runtime)
    manager.add_provider(provider)

    assert [row["name"] for row in manager.get_all_tool_schemas()] == ["memory_read"]
    assert manager.prefetch_all("private query", memory_provenance=object()) == ""
    manager.queue_prefetch_all("private query", memory_provenance=object())
    manager.sync_all("user", "assistant", memory_provenance=object())
    manager.on_turn_start(1, "private message", memory_provenance=object())
    manager.on_session_end([{"role": "user", "content": "private"}], memory_provenance=object())
    assert manager.on_pre_compress([], memory_provenance=object()) == ""
    assert provider.calls == []

    result = json.loads(
        manager.handle_tool_call(
            "memory_read",
            {"query": "bounded"},
            memory_provenance=object(),
            tool_call_id="tool-1",
        )
    )
    assert result["data_class"] == "tainted_provider_data"
    assert [call[0] for call in provider.calls] == ["tool"]
    assert runtime.authorizations[0]["tool_call_id"] == "tool-1"


def test_strict_manager_denies_capability_writes_and_provider_errors_content_safely() -> None:
    denied_runtime = StrictRuntime(allowed=False)
    denied_provider = RecordingProvider()
    denied = MemoryManager(memory_runtime=denied_runtime)
    denied.add_provider(denied_provider)
    result = json.loads(
        denied.handle_tool_call(
            "memory_read", {}, memory_provenance=object(), tool_call_id="tool-denied"
        )
    )
    assert result["status"] == "denied"
    assert result["error"]["code"] == "memory_subject_denied"
    assert denied_provider.calls == []

    write_denial = json.loads(
        denied.authorize_builtin_memory_tool(
            {"action": "add", "content": "must not land"},
            memory_provenance=object(),
            tool_call_id="tool-write",
        )
    )
    assert write_denial["error"]["code"] == "governed_write_denied"

    failing = MemoryManager(memory_runtime=StrictRuntime())
    failing.add_provider(RecordingProvider(fail=True))
    contained = failing.handle_tool_call(
        "memory_read", {}, memory_provenance=object(), tool_call_id="tool-error"
    )
    assert "memory_provider_unavailable" in contained
    assert "secret provider response fragment" not in contained


def test_unconfigured_manager_preserves_legacy_prefetch_and_write_behavior() -> None:
    provider = RecordingProvider()
    manager = MemoryManager()
    manager.add_provider(provider)

    assert manager.prefetch_all("legacy query") == "legacy context"
    assert manager.authorize_builtin_memory_tool({"action": "add"}) is None
