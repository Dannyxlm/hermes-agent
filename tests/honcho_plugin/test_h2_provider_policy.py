from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.memory_provenance import (
    issue_authenticated_origin,
    issue_memory_tool_receipt,
    issue_memory_write_receipt,
    issue_turn_envelope,
)
from plugins.memory.honcho import HonchoMemoryProvider


def _provider(*, deadline: float = 0.2):
    provider = HonchoMemoryProvider()
    provider._recall_mode = "tools"
    provider._session_key = "telegram:session"
    provider._session_initialized = True
    provider._capability_verified = True
    provider._manager = MagicMock()
    provider._config = SimpleNamespace(
        provider_state="tools_only",
        provider_state_explicit=True,
        tool_deadline_seconds=deadline,
        eligible_profiles=["default"],
        trusted_principal_ids=["fixture-danny"],
        allow_local_cli_writes=True,
        policy_revision="memory-source-policy/v1",
        writer_release="hermes-memory-boundary/v1",
        reasoning_deadline_seconds=0.5,
        reasoning_estimated_cost_usd=0.02,
        reasoning_receipt_path="/fixture/reasoning.jsonl",
    )
    provider._reasoning_public_receipts = []
    provider._reasoning_receipt_appender = (
        lambda _path, receipt: provider._reasoning_public_receipts.append(receipt) or True
    )
    return provider


def _tool_authority(tool_name: str, call_id: str, *, principal: str = "fixture-danny"):
    origin = issue_authenticated_origin(
        runtime_class="gateway",
        origin_class="authenticated_human_gateway",
        platform="telegram",
        profile="default",
        principal_id=principal,
        adapter_receipt_id="adapter-receipt",
        source_observation_id="source-observation",
    )
    envelope = issue_turn_envelope(
        origin,
        session_id="telegram:session",
        turn_id=f"turn-{call_id}",
        message_id=f"message-{call_id}",
        user_content="fixture turn",
    )
    return issue_memory_tool_receipt(
        envelope,
        tool_name=tool_name,
        tool_call_id=call_id,
    )


def _write_authority(call_id: str, content: str, *, principal: str = "fixture-danny"):
    origin = issue_authenticated_origin(
        runtime_class="gateway",
        origin_class="authenticated_human_gateway",
        platform="telegram",
        profile="default",
        principal_id=principal,
        adapter_receipt_id="adapter-write",
        source_observation_id="observation-write",
    )
    envelope = issue_turn_envelope(
        origin,
        session_id="telegram:session",
        turn_id=f"turn-{call_id}",
        message_id=f"message-{call_id}",
        user_content="remember this",
    )
    return issue_memory_write_receipt(
        envelope,
        tool_call_id=call_id,
        action="add",
        target="user",
        content=content,
        committed=True,
    )


def test_profile_is_canonical_read_only_bounded_and_tui_readable():
    provider = _provider()
    provider._manager.strict_peer_card.return_value = [f"fact-{i}-" + "x" * 500 for i in range(30)]

    response = json.loads(provider.handle_tool_call("honcho_profile", {"peer": "user"}))
    assert response["status"] == "ok"
    assert len(response["result"]) <= 16
    assert sum(map(len, response["result"])) <= 2400
    provider._manager.strict_peer_card.assert_called_once()

    denied_scope = provider.handle_tool_call("honcho_profile", {"peer": "other-person-id"})
    assert "canonical peer" in denied_scope
    denied_write = provider.handle_tool_call(
        "honcho_profile", {"peer": "user", "card": ["overwrite"]}
    )
    assert "mutation is disabled" in denied_write
    assert provider._manager.set_peer_card.call_count == 0


def test_search_and_context_use_only_strict_bounded_manager_methods():
    provider = _provider()
    provider._manager.strict_human_search.return_value = [
        {"id": str(i), "session_id": "s", "content": "x" * 800}
        for i in range(20)
    ]
    provider._manager.strict_peer_context.return_value = {
        "representation": "r" * 5000,
        "card": ["c" * 500 for _ in range(20)],
    }

    search = json.loads(provider.handle_tool_call(
        "honcho_search", {"query": "specific fact", "max_tokens": 1000, "peer": "user"}
    ))
    assert search["count"] <= 8
    assert sum(len(value) for row in search["results"] for value in row.values()) <= 4000
    context = json.loads(provider.handle_tool_call("honcho_context", {"peer": "user"}))
    assert len(context["result"]["representation"]) <= 2400
    assert sum(map(len, context["result"]["card"])) <= 1200
    provider._manager.search_context.assert_not_called()
    provider._manager.get_session_context.assert_not_called()


def test_conclusion_creation_requires_trusted_receipt_and_is_single_use():
    provider = _provider()
    provider._manager.strict_create_conclusion.return_value = True

    denied = provider.handle_tool_call(
        "honcho_conclude", {"conclusion": "stable user fact", "peer": "user"},
        tool_call_id="call-missing",
    )
    assert "trusted executor receipt" in denied
    provider._manager.strict_create_conclusion.assert_not_called()

    call_id = "call-conclude"
    receipt = _tool_authority("honcho_conclude", call_id)
    allowed = json.loads(provider.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "stable user fact", "peer": "user"},
        tool_call_id=call_id,
        tool_receipt=receipt,
    ))
    assert allowed == {"status": "ok", "result": "Conclusion saved."}
    provider._manager.strict_create_conclusion.assert_called_once()

    replay = provider.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "stable user fact", "peer": "user"},
        tool_call_id=call_id,
        tool_receipt=receipt,
    )
    assert "trusted executor receipt" in replay
    assert provider._manager.strict_create_conclusion.call_count == 1


def test_other_group_principal_and_destructive_delete_are_denied_before_remote_call():
    provider = _provider()
    receipt = _tool_authority(
        "honcho_conclude", "call-other", principal="other-group-member"
    )
    denied = provider.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "not authoritative", "peer": "user"},
        tool_call_id="call-other",
        tool_receipt=receipt,
    )
    assert "trusted executor receipt" in denied
    deletion = provider.handle_tool_call(
        "honcho_conclude", {"delete_id": "opaque-id", "peer": "user"}
    )
    assert "separate approved maintenance action" in deletion
    provider._manager.strict_create_conclusion.assert_not_called()
    provider._manager.delete_conclusion.assert_not_called()


def test_paid_reasoning_allows_one_bounded_call_and_denies_high_or_replay():
    provider = _provider()
    provider._manager.strict_reasoning_call.return_value = "answer" * 2000

    high = provider.handle_tool_call(
        "honcho_reasoning", {"query": "question", "reasoning_level": "high"}
    )
    assert "only minimal, low, or medium" in high
    provider._manager.strict_reasoning_call.assert_not_called()

    call_id = "call-reasoning"
    receipt = _tool_authority("honcho_reasoning", call_id)
    response = json.loads(provider.handle_tool_call(
        "honcho_reasoning",
        {"query": "question", "reasoning_level": "low", "peer": "user"},
        tool_call_id=call_id,
        tool_receipt=receipt,
    ))
    assert response["status"] == "ok"
    assert len(response["result"]) <= 4000
    provider._manager.strict_reasoning_call.assert_called_once()
    assert [r["phase"] for r in provider._reasoning_public_receipts] == [
        "reserved", "terminal"
    ]
    assert "question" not in json.dumps(provider._reasoning_public_receipts)

    replay = provider.handle_tool_call(
        "honcho_reasoning",
        {"query": "question", "reasoning_level": "low", "peer": "user"},
        tool_call_id=call_id,
        tool_receipt=receipt,
    )
    assert "trusted executor receipt" in replay
    assert provider._manager.strict_reasoning_call.call_count == 1
    assert len(provider._reasoning_public_receipts) == 2


def test_paid_reasoning_never_calls_backend_when_reservation_receipt_fails():
    provider = _provider()
    provider._reasoning_receipt_appender = lambda _path, _receipt: False
    receipt = _tool_authority("honcho_reasoning", "call-no-reservation")
    response = provider.handle_tool_call(
        "honcho_reasoning",
        {"query": "question", "reasoning_level": "low"},
        tool_call_id="call-no-reservation",
        tool_receipt=receipt,
    )
    assert "no call was made" in response
    provider._manager.strict_reasoning_call.assert_not_called()


def test_paid_reasoning_reports_terminal_receipt_failure_after_single_call():
    provider = _provider()
    writes = []

    def appender(_path, receipt):
        writes.append(receipt)
        return len(writes) == 1

    provider._reasoning_receipt_appender = appender
    provider._manager.strict_reasoning_call.return_value = "answer"
    receipt = _tool_authority("honcho_reasoning", "call-terminal-fail")
    response = json.loads(provider.handle_tool_call(
        "honcho_reasoning",
        {"query": "question", "reasoning_level": "low"},
        tool_call_id="call-terminal-fail",
        tool_receipt=receipt,
    ))
    assert response["code"] == "terminal_receipt_persistence_failed"
    provider._manager.strict_reasoning_call.assert_called_once()
    assert [item["phase"] for item in writes] == ["reserved", "terminal"]


def test_cheap_tool_deadline_is_typed_and_does_not_leak_backend_value():
    provider = _provider(deadline=0.01)

    def slow(*_args, **_kwargs):
        time.sleep(0.2)
        return ["late private value"]

    provider._manager.strict_peer_card.side_effect = slow
    started = time.monotonic()
    response_text = provider.handle_tool_call("honcho_profile", {"peer": "user"})
    elapsed = time.monotonic() - started
    response = json.loads(response_text)
    assert elapsed < 0.15
    assert response == {"status": "degraded", "code": "deadline_exceeded", "result": None}
    assert "late private value" not in response_text


def test_builtin_memory_mirror_requires_committed_write_receipt_and_rejects_replay():
    provider = _provider()
    provider._manager.strict_create_conclusion.return_value = True
    content = "User prefers concise answers"

    provider.on_memory_write("add", "user", content)
    time.sleep(0.02)
    provider._manager.strict_create_conclusion.assert_not_called()

    receipt = _write_authority("memory-call-1", content)
    provider.on_memory_write("add", "user", content, write_receipt=receipt)
    deadline = time.monotonic() + 0.5
    while (
        provider._manager.strict_create_conclusion.call_count < 1
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    provider._manager.strict_create_conclusion.assert_called_once_with(
        "telegram:session",
        content,
        peer="user",
    )

    provider.on_memory_write("add", "user", content, write_receipt=receipt)
    time.sleep(0.02)
    assert provider._manager.strict_create_conclusion.call_count == 1


def test_builtin_memory_mirror_rejects_non_authoritative_group_principal():
    provider = _provider()
    content = "Other group member claim"
    receipt = _write_authority(
        "memory-call-other",
        content,
        principal="other-group-member",
    )
    provider.on_memory_write("add", "user", content, write_receipt=receipt)
    time.sleep(0.02)
    provider._manager.strict_create_conclusion.assert_not_called()
