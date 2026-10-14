"""Human producers and exact-owner pending waits survive WS detachment."""
import threading
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import approval
from tools.approval_gateway_wait import _ApprovalNotice
from tools.connectors import live
from tui_gateway import server, server_requests


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_pending_ws_reaps", {})
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(server_requests, "_open", {})
    # Registry policy is parent-owned. These lifecycle unit tests exercise its
    # exact-owner live/dead query boundary, not duplicate its deadline engine.
    monkeypatch.setattr(server_requests, "has_pending_human",
                        lambda sid: any(req.sid == sid for req in server_requests._open.values()), raising=False)
    live.reset_for_tests()
    yield
    live.reset_for_tests()


def test_approval_notifier_passes_exact_frozen_deadline_and_deny_reason(monkeypatch):
    server._sessions["sid"] = {"session_key": "owner"}
    captured = {}
    def send(method, sid, params, callback, **kwargs):
        captured.update(method=method, params=params, callback=callback, **kwargs)
        return lambda reason: None
    monkeypatch.setattr(server_requests, "send_async", send)
    monkeypatch.setattr(approval, "register_gateway_settle", lambda *args: True)
    resolve = Mock()
    monkeypatch.setattr(approval, "resolve_gateway_approval", resolve)
    server._emit_approval_request("sid", _ApprovalNotice({"command": "review", "request_id": "queue-id",
        "created_at": 1000.0, "expires_at": 1300.0, "timeout_seconds": 300.0}, 310.0))
    assert captured["timeout"] == 300
    assert captured["created_at"] == 1000
    assert captured["deadline_monotonic"] == 310
    assert "_deadline_monotonic" not in captured["params"]
    captured["callback"]({"choice": "deny", "reason": "not this origin"})
    assert resolve.call_args.kwargs["reason"] == "not this origin"


def test_registry_expiry_does_not_misattribute_an_unanswerable_client(monkeypatch):
    server._sessions["sid"] = {"session_key": "owner"}
    callback = []
    monkeypatch.setattr(server_requests, "send_async",
                        lambda method, sid, params, cb, **kw: callback.append(cb) or (lambda reason: None))
    monkeypatch.setattr(approval, "register_gateway_settle", lambda *args: True)
    withdraw = Mock()
    monkeypatch.setattr(approval, "withdraw_gateway_approval", withdraw)
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0)
    server._emit_approval_request("sid", _ApprovalNotice({"command": "review", "request_id": "queue-id",
        "timeout_seconds": 300.0, "created_at": 1000.0}, 100.0))
    callback[0](None)
    # The queue's own frozen deadline must produce timeout, not cancelled with
    # an invented "update the app" cause from the registry's expiry callback.
    withdraw.assert_not_called()


def test_secure_callbacks_share_policy_without_shortening_clarify(monkeypatch):
    from agent.vault_backends import unlock
    from tools import terminal_tool, skills_tool

    captures = []
    callbacks = {}
    monkeypatch.setattr(server, "_ask", lambda method, sid, params, **kw: captures.append((method, kw)) or "")
    monkeypatch.setattr(terminal_tool, "set_sudo_password_callback", lambda cb: callbacks.update(sudo=cb))
    monkeypatch.setattr(skills_tool, "set_secret_capture_callback", lambda cb: callbacks.update(secret=cb))
    monkeypatch.setattr(unlock, "set_unlock_prompt_callback", lambda cb: callbacks.update(unlock=cb))
    monkeypatch.setattr(unlock, "set_save_login_prompt_callback", lambda cb: callbacks.update(save=cb))
    monkeypatch.setattr(unlock, "set_code_prompt_callback", lambda cb: callbacks.update(code=cb))
    monkeypatch.setattr(unlock, "set_current_session_id", lambda sid: None)
    # The shared timeout resolver is owned by the canonical-registry unit;
    # producers must use it instead of hardcoded 120/180 second budgets.
    policy = Mock(return_value=421)
    monkeypatch.setitem(sys.modules, "tui_gateway.human_input", SimpleNamespace(human_timeout=policy))
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key: "sid")
    server._sessions["sid"] = {"session_key": "owner"}
    server._wire_callbacks("sid")
    callbacks["sudo"]()
    callbacks["secret"]("EXAMPLE_KEY", "Enter key")
    callbacks["unlock"]("bitwarden", "Manager")
    callbacks["save"]("https://example.com", "Example")
    callbacks["code"]("Example", "SMS")
    assert {method: kwargs.get("timeout") for method, kwargs in captures} == {
        "sudo": 421, "secret": 421, "vault.unlock_prompt": 421,
        "vault.save_login": 421, "vault.code": 421}
    assert policy.call_count == 5
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: None)
    send = Mock(return_value={"picked": "ok"})
    monkeypatch.setattr(server_requests, "send", send)
    server._setup_choose_request("sid", {"kind": "question", "question": "Choose"})
    assert send.call_args.kwargs["timeout"] is None


@pytest.fixture
def reaper(monkeypatch):
    callbacks, interrupted, reaped = [], [], []
    class Timer:
        def __init__(self, delay, callback):
            callbacks.append(callback)
        def start(self):
            pass
        def cancel(self):
            pass
    monkeypatch.setattr(server.threading, "Timer", Timer)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 20)
    monkeypatch.setattr(server, "_ws_orphan_turn_activity_is_fresh", lambda s: False)
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda *a: False)
    monkeypatch.setattr(server, "_session_owns_live_wakeup_schedule", lambda *a: False)
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda sid, s, **kw: interrupted.append(sid))
    monkeypatch.setattr(server, "_teardown_popped_session", lambda s, **kw: reaped.append(s) if s else None)
    return callbacks, interrupted, reaped


def session(running=True):
    return {"session_key": "owner", "history_lock": threading.RLock(), "running": running,
            "transport": server._detached_ws_transport}


@pytest.mark.parametrize("kind", ["registry", "compute", "connector"])
def test_live_human_wait_defers_reap_but_expiry_removes_exemption(monkeypatch, reaper, kind):
    callbacks, interrupted, reaped = reaper
    current = session()
    server._sessions["sid"] = current
    now = server.time.time()
    if kind == "registry":
        req = server_requests.ServerRequest("sid", "approval", {"command": "review", "choices": ["once", "deny"]})
        server_requests._open[req.id] = req
        expire = lambda: server_requests._open.clear()
    elif kind == "compute":
        current["_compute_host_turn_id"] = "turn"
        current["_compute_host_open_request"] = {"id": "srq-child", "method": "secret",
            "params": {"session_id": "sid"}, "expires_at": now + 300}
        expire = lambda: current["_compute_host_open_request"].update(expires_at=now - 1)
    else:
        operation = SimpleNamespace(settled=False, deadline_at=now + 300)
        monkeypatch.setattr(live, "current", lambda key, **kw: operation if key == "owner" else None)
        expire = lambda: setattr(operation, "deadline_at", now - 1)
    server._schedule_ws_orphan_reap("sid")
    callbacks.pop(0)()
    assert interrupted == [] and reaped == []
    assert "_client_gone_interrupt_requested" not in current
    assert len(callbacks) == 1
    expire()
    callbacks.pop(0)()
    assert interrupted == ["sid"]


@pytest.mark.parametrize("mirror", [None, "expired", "surface", "foreign", "unlimited", "legacy"])
def test_unrelated_or_nonhuman_or_expired_mirror_does_not_pin_orphan(monkeypatch, reaper, mirror):
    callbacks, interrupted, reaped = reaper
    current = session(running=False)
    server._sessions["sid"] = current
    # Another session's live question is not an exemption for this orphan.
    req = server_requests.ServerRequest("other", "secret", {"env_var": "KEY", "prompt": "Enter"})
    server_requests._open[req.id] = req
    if mirror:
        current["_compute_host_turn_id"] = "turn"
        current["_compute_host_open_request"] = {"id": "srq-child",
            "method": "clarify" if mirror == "legacy" else "preview.read" if mirror == "surface" else "sudo",
            "params": {"session_id": "foreign" if mirror == "foreign" else "sid"},
            "expires_at": None if mirror == "unlimited" else server.time.time() + (-1 if mirror == "expired" else 300)}
        if mirror == "legacy":
            current["_compute_host_open_request"].pop("expires_at")
    server._schedule_ws_orphan_reap("sid")
    callbacks.pop(0)()
    assert interrupted == []
    assert reaped == [current]
    assert "sid" not in server._sessions


def test_connector_exemption_is_profile_scoped_and_stop_removes_it(monkeypatch, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.connectors.operation import ConnectionOperation
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    operation = ConnectionOperation([], session_key="owner")
    token = set_hermes_home_override(first)
    try:
        live.open(operation)
    finally:
        reset_hermes_home_override(token)
    current = session()
    current["profile_home"] = str(second)
    assert server._session_has_pending_human("sid", current) is False
    current["profile_home"] = str(first)
    assert server._session_has_pending_human("sid", current) is True
    current["_turn_cancel_requested"] = True
    assert server._session_has_pending_human("sid", current) is False


def test_explicit_unlimited_clarify_mirror_survives_without_a_false_secure_unlimited_wait():
    current = session()
    current["_compute_host_turn_id"] = "turn"
    current["_compute_host_open_request"] = {"id": "srq-child", "method": "clarify",
        "params": {"session_id": "sid"}, "expires_at": None}
    assert server._session_has_pending_human("sid", current) is True
    current["_compute_host_open_request"]["method"] = "sudo"
    assert server._session_has_pending_human("sid", current) is False
