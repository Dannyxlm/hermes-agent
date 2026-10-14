"""Danny's KEEP decision: ``/approve always|session`` on a Smart-DENY request is a one-time owner override.

The canonical resolver settles it as ``once`` for that exact request and persists no session or
permanent grant. Consent kinds (payment, MCP trust/elicitation) still refuse reusable scopes even when
flagged Smart-DENY, and bulk approval still skips payments.
"""

import pytest

from tools import approval as A
from tools import approval_context, approval_detection, approval_smart
from tools.approval_gateway_wait import _ApprovalEntry

PATTERN = "smart-deny-override-scope"


@pytest.fixture
def smart_deny_session(monkeypatch):
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 5)  # a regression fails fast
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_smart, "_smart_approve", lambda _command, _description: "deny")
    for module in (A, approval_detection):
        monkeypatch.setattr(module, "detect_dangerous_command", lambda command: (True, PATTERN, f"risk:{command}"))
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    session_key = "smart-deny-override-scope-session"
    token = approval_context.set_current_session_key(session_key)
    persisted = []
    monkeypatch.setattr(A, "save_permanent_allowlist", lambda patterns: persisted.append(set(patterns)))
    with A._lock:
        A._gateway_queues.pop(session_key, None)
        A._permanent_approved.discard(PATTERN)
        A._session_approved.pop(session_key, None)
    try:
        yield session_key, persisted
    finally:
        approval_context.reset_current_session_key(token)
        with A._lock:
            A._gateway_queues.pop(session_key, None)
            A._gateway_notify_cbs.pop(session_key, None)
            A._session_approved.pop(session_key, None)
            A._permanent_approved.discard(PATTERN)


@pytest.mark.parametrize("choice", ["always", "session"])
def test_canonical_broad_scope_is_one_time_override_with_no_persisted_grant(smart_deny_session, choice):
    session_key, persisted = smart_deny_session
    seen = {}

    def notify(data):
        seen["data"] = dict(data)
        seen["count"] = A.resolve_gateway_approval(session_key, choice, request_id=data["request_id"])

    with A._lock:
        A._gateway_notify_cbs[session_key] = notify
    result = A.check_all_command_guards("dangerous /tmp/override", "local")

    assert seen["count"] == 1
    assert result["approved"] is True and result["user_approved"] is True
    assert seen["data"]["smart_denied"] is True and seen["data"]["choices"] == ["once", "deny"]
    # One operation only: nothing persisted in memory or to config.
    assert A.is_approved(session_key, PATTERN) is False
    assert PATTERN not in A._permanent_approved and PATTERN not in A._session_approved.get(session_key, set())
    assert persisted == []

    # The next identical command prompts again; a deny there is honoured.
    def deny(data):
        A.resolve_gateway_approval(session_key, "deny", request_id=data["request_id"])

    with A._lock:
        A._gateway_notify_cbs[session_key] = deny
    again = A.check_all_command_guards("dangerous /tmp/override", "local")
    assert again["approved"] is False and again["outcome"] == "denied"


@pytest.mark.parametrize("surface", ["vault-payment", "mcp-trust/srv", "mcp-elicitation/srv"])
@pytest.mark.parametrize("choice", ["always", "session"])
def test_consent_kinds_still_refuse_reusable_scope_even_when_smart_denied(monkeypatch, surface, choice):
    key = "synthetic:smart-deny-consent"
    entry = _ApprovalEntry({"surface": surface, "request_id": "consent-1", "smart_denied": True})
    monkeypatch.setattr(A, "_gateway_queues", {key: [entry]})

    assert A.resolve_gateway_approval(key, choice, request_id="consent-1") == 0
    assert A.resolve_gateway_approval(key, choice) == 0
    assert entry.result is None and not entry.event.is_set()
    assert A.list_gateway_approvals(key) == [entry.data]
    assert A.resolve_gateway_approval(key, "once", request_id="consent-1") == 1
    assert entry.result == "once"


@pytest.mark.parametrize("choice", ["always", "session"])
def test_bulk_broad_scope_settles_smart_deny_once_and_still_skips_payment(monkeypatch, choice):
    key = "synthetic:smart-deny-bulk"
    payment = _ApprovalEntry({"surface": "vault-payment", "request_id": "pay-1", "smart_denied": True})
    smart = _ApprovalEntry({"surface": "gateway", "request_id": "shell-1", "smart_denied": True,
                            "allow_session": False, "allow_permanent": False})
    ordinary = _ApprovalEntry({"surface": "gateway", "request_id": "shell-2"})
    monkeypatch.setattr(A, "_gateway_queues", {key: [payment, smart, ordinary]})

    assert A.resolve_gateway_approval(key, choice, resolve_all=True) == 2
    assert smart.result == "once" and ordinary.result == choice
    assert payment.result is None and not payment.event.is_set()
    assert A.list_gateway_approvals(key) == [payment.data]
