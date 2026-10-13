"""Frozen approval deadlines and per-invocation consent ownership."""
import contextlib
from unittest.mock import Mock

import pytest

from tools import approval, approval_gateway_wait as wait, approval_prompt as prompt


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(wait._ctx, "_fire_approval_hook", lambda *a, **k: None)
    monkeypatch.setattr(wait, "is_interrupted", lambda: False)
    monkeypatch.setattr(wait, "human_wait_window", lambda *args: contextlib.nullcontext())


def test_resolver_refuses_late_and_unoffered_choices_before_consumption(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(wait.time, 'monotonic', lambda: clock[0])
    entry = wait._ApprovalEntry({'allow_session': False}, timeout_seconds=300)
    approval._gateway_queues['owner'] = [entry]
    assert approval.resolve_gateway_approval('owner', 'always', request_id=entry.data['request_id']) == 0
    assert entry.result is None and not entry.event.is_set()
    clock[0] = 310.0
    assert approval.resolve_gateway_approval('owner', 'once', request_id=entry.data['request_id']) == 0
    assert entry.result is None


def test_deadline_is_frozen_before_notifier_and_not_reloaded(monkeypatch):
    clock = [10.0]
    timeout = Mock(return_value=300)
    monkeypatch.setattr(wait.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(wait.time, "time", lambda: 1000.0)
    monkeypatch.setattr(wait._ctx, "_get_approval_timeout", timeout)
    published = []

    def notify(data):
        published.append(data)
        # Publication/hook latency consumes this request's budget; a config
        # edit cannot extend or shorten the already-created human question.
        clock[0] = 309.0
        timeout.return_value = 1
        entry = approval._gateway_queues["owner"][0]

        def answer(*, timeout):
            assert timeout == 1.0
            approval.resolve_gateway_approval("owner", "once")
            return True

        monkeypatch.setattr(entry.event, "wait", answer)

    decision = wait._await_gateway_decision("owner", notify, {"command": "review", "pattern_keys": ["x"]})
    assert decision["choice"] == "once"
    assert timeout.call_count == 1
    assert published[0]["created_at"] == 1000.0
    assert published[0]["expires_at"] == 1300.0
    assert published[0]["timeout_seconds"] == 300
    assert published[0].deadline_monotonic == 310.0
    assert "deadline_monotonic" not in published[0]


def test_publication_latency_does_not_restart_budget(monkeypatch):
    clock = [20.0]
    monkeypatch.setattr(wait.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(wait._ctx, "_get_approval_timeout", lambda: 300)

    def notify(data):
        clock[0] = 320.0
        monkeypatch.setattr(approval._gateway_queues["owner"][0].event, "wait",
                            lambda **kw: pytest.fail("expired producer deadline must not wait again"))

    result = wait._await_gateway_decision("owner", notify, {"command": "review"})
    assert result == {"resolved": False, "choice": None, "reason": None}
    assert "owner" not in approval._gateway_queues


def test_once_only_requests_never_coalesce_with_existing_consent(monkeypatch):
    leader = wait._ApprovalEntry({"command": "confirm", "pattern_keys": ["mcp_elicitation"],
                                  "allow_session": False, "allow_permanent": False})
    approval._gateway_queues["owner"] = [leader]
    monkeypatch.setattr(wait, "_await_coalesced_leader", lambda *a: pytest.fail("per-call consent cannot coalesce"))

    def notify(data):
        assert data["choices"] == ["once", "deny"]
        assert len(approval._gateway_queues["owner"]) == 2
        approval.resolve_gateway_approval("owner", "once", request_id=data["request_id"])

    result = wait._await_gateway_decision("owner", notify, {"command": "confirm",
        "pattern_keys": ["mcp_elicitation"], "allow_session": False, "allow_permanent": False})
    assert result["choice"] == "once"
    assert approval._gateway_queues["owner"] == [leader]


@pytest.mark.parametrize("gateway", [True, False])
def test_elicitation_is_once_only_and_honors_explicit_timeout(monkeypatch, gateway):
    monkeypatch.setattr(prompt._ctx, "get_current_session_key", lambda: "owner")
    monkeypatch.setattr(prompt._ctx, "_get_session_platform", lambda: "tui")
    monkeypatch.setattr(prompt._ctx, "_is_gateway_approval_context", lambda: gateway)
    monkeypatch.setattr(prompt._ctx, "_no_user_can_answer", lambda: False)
    if gateway:
        monkeypatch.setattr(approval, "_gateway_notify_cb", lambda sk: lambda data: None)
        def ask_gateway(sk, cb, data, **kwargs):
            assert data["allow_session"] is False
            assert data["allow_permanent"] is False
            assert kwargs["timeout_seconds"] == 47
            return {"resolved": True, "choice": "once"}
        monkeypatch.setattr(wait, "_await_gateway_decision", ask_gateway)
    else:
        def ask_cli(*args, **kwargs):
            assert kwargs["allow_session"] is False
            assert kwargs["allow_permanent"] is False
            assert kwargs["timeout_seconds"] == 47
            return "once"
        monkeypatch.setattr(prompt, "prompt_dangerous_approval", ask_cli)
    assert prompt.request_elicitation_consent("confirm", "description", timeout_seconds=47) == "accept"


def test_restricted_follower_does_not_adopt_a_permanent_grant(monkeypatch):
    leader = wait._ApprovalEntry({"command": "review", "pattern_keys": ["x"]})
    approval._gateway_queues["owner"] = [leader]
    monkeypatch.setattr(wait, "_await_coalesced_leader", lambda *a: pytest.fail("different consent scopes cannot coalesce"))
    def notify(data):
        assert data["choices"] == ["once", "session", "deny"]
        approval.resolve_gateway_approval("owner", "once", request_id=data["request_id"])
    assert wait._await_gateway_decision("owner", notify, {"command": "review", "pattern_keys": ["x"],
        "allow_permanent": False})["choice"] == "once"


@pytest.mark.parametrize("choice", ["session", "always"])
def test_per_call_consent_rejects_unoffered_broad_response(choice):
    assert prompt._consent(choice, "cancel") == "decline"
