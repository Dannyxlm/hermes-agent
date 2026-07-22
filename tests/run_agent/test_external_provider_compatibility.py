"""Compatibility contract for standalone external memory providers.

These tests intentionally use a generic provider. Hermes owns the lifecycle
and routing contract; provider-specific authority and storage behavior belong
in the provider package itself.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.memory_manager import (
    MemoryManager,
    inject_memory_provider_tools,
    memory_provider_owns_tool,
)
from agent.memory_provider import MemoryProvider


def _tool_schema(name: str) -> dict:
    return {
        "name": name,
        "description": f"Run {name}",
        "parameters": {"type": "object", "properties": {}},
    }


class LifecycleProvider(MemoryProvider):
    name = "external-test"

    def __init__(self, *, before: list[str], after: list[str]) -> None:
        self._before = before
        self._after = after
        self._initialized = False
        self.prefetch_calls: list[tuple[str, str]] = []
        self.sync_calls: list[tuple[str, str, str]] = []

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._initialized = True

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.prefetch_calls.append((query, session_id))
        return "recalled context"

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages=None,
    ) -> None:
        self.sync_calls.append((user_content, assistant_content, session_id))

    def get_tool_schemas(self) -> list[dict]:
        names = self._after if self._initialized else self._before
        return [_tool_schema(name) for name in names]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        return json.dumps({"tool": tool_name})


def test_initialize_refreshes_late_bound_provider_tools():
    provider = LifecycleProvider(before=[], after=["external_recall"])
    manager = MemoryManager()
    manager.add_provider(provider)

    assert not manager.has_tool("external_recall")

    manager.initialize_all("session-1", platform="cli")

    assert manager.has_tool("external_recall")
    assert [schema["name"] for schema in manager.get_all_tool_schemas()] == [
        "external_recall"
    ]
    assert json.loads(manager.handle_tool_call("external_recall", {})) == {
        "tool": "external_recall"
    }


def test_initialize_removes_tools_provider_no_longer_exposes():
    provider = LifecycleProvider(before=["bootstrap_probe"], after=[])
    manager = MemoryManager()
    manager.add_provider(provider)

    assert manager.has_tool("bootstrap_probe")

    manager.initialize_all("session-1", platform="cli")

    assert not manager.has_tool("bootstrap_probe")
    assert manager.get_all_tool_schemas() == []


def test_late_bound_provider_tool_cannot_claim_registry_collision():
    provider = LifecycleProvider(before=[], after=["registry_search"])
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("session-1", platform="cli")
    agent = SimpleNamespace(
        _memory_manager=manager,
        _memory_provider_tool_names=set(),
        enabled_toolsets=None,
        tools=[
            {
                "type": "function",
                "function": _tool_schema("registry_search"),
            }
        ],
        valid_tool_names={"registry_search"},
    )

    assert inject_memory_provider_tools(agent) == 0
    assert agent._memory_provider_tool_names == set()
    assert not memory_provider_owns_tool(agent, "registry_search")


def test_late_bound_unique_tool_is_advertised_and_owned():
    provider = LifecycleProvider(before=[], after=["external_recall"])
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("session-1", platform="cli")
    agent = SimpleNamespace(
        _memory_manager=manager,
        _memory_provider_tool_names=set(),
        enabled_toolsets=None,
        tools=[],
        valid_tool_names=set(),
    )

    assert inject_memory_provider_tools(agent) == 1
    assert agent._memory_provider_tool_names == {"external_recall"}
    assert memory_provider_owns_tool(agent, "external_recall")


def test_prefetch_does_not_implicitly_write_a_turn():
    provider = LifecycleProvider(before=[], after=[])
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("session-1", platform="cli")

    assert manager.prefetch_all("what matters?", session_id="session-1") == (
        "recalled context"
    )
    assert provider.prefetch_calls == [("what matters?", "session-1")]
    assert provider.sync_calls == []

    manager.sync_all("question", "answer", session_id="session-1")
    assert manager.flush_pending(timeout=5)
    assert provider.sync_calls == [("question", "answer", "session-1")]
