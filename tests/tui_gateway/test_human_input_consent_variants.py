"""Consent variants use the real producer, canonical queue and request resolver."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from tools import approval, approval_context, approval_prompt
from tools.approval_gateway_wait import _await_gateway_decision
from tui_gateway import server, server_requests as sr
from tui_gateway.contracts.server_requests import ApprovalRequestParams


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_sessions", {"runtime": {"session_key": "owner"}})
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(approval, "_gateway_notify_cbs", {})
    sr.reset_for_tests()
    frames = []
    sr.bind_sinks(frames.append, lambda *args: None, lambda sid: True)
    yield frames
    sr.cancel()
    sr.reset_for_tests()


@pytest.mark.parametrize("surface,extra,kind,target", [
    ("gateway", {"tool_name": "terminal", "workspace": "/project"}, "terminal", None),
    ("vault-payment", {"origin": "https://checkout.example"}, "payment", "https://checkout.example"),
    ("mcp-trust/accounting", {}, "mcp_trust", "accounting"),
    ("mcp-elicitation/accounting", {}, "mcp_elicitation", "accounting"),
    ("gateway", {"kind": "payment"}, "payment", None),
    ("gateway", {"kind": "mcp_trust"}, "mcp_trust", None),
    ("gateway", {"kind": "mcp_elicitation"}, "mcp_elicitation", None),
])
def test_typed_context_and_once_only_offers(surface, extra, kind, target):
    payload = server._approval_request_payload({"request_id": "queue", "surface": surface, **extra})
    model = ApprovalRequestParams.model_validate({"session_id": "runtime", **payload})
    assert model.kind == kind
    if kind != "terminal":
        assert payload["choices"] == ["once", "deny"]
        assert payload["allow_session"] is False
        assert payload["allow_permanent"] is False
    if kind == "payment":
        assert model.origin == target
    elif kind.startswith("mcp_"):
        assert model.mcp_server == target


@pytest.mark.parametrize("surface", ["vault-payment", "mcp-trust/accounting", "mcp-elicitation/accounting"])
def test_forged_reusable_consent_is_rejected_before_consumption(surface):
    values = {"request_id": "forged", "surface": surface,
              "choices": ["once", "session", "always", "deny"]}
    with pytest.raises(ValidationError):
        ApprovalRequestParams.model_validate({"session_id": "runtime", **values})
    seen = []
    sr.send_async("approval", "runtime", server._approval_request_payload(values), seen.append)
    request = sr.open_requests("runtime")[0]
    for choice in ("session", "always"):
        with pytest.raises(PermissionError):
            sr.resolve_response({"id": request["id"], "result": {"choice": choice}})
    assert seen == []
    assert sr.open_requests("runtime")


@pytest.mark.parametrize("context", [
    {"surface": "vault-payment"}, {"surface": "mcp-trust/dummy"},
    {"surface": "mcp-elicitation/dummy"}, {"kind": "payment"},
    {"kind": "mcp_trust"}, {"kind": "mcp_elicitation"},
])
def test_internal_unnormalized_variants_refuse_registration_and_reusable_answers(context):
    from tui_gateway.human_input import validate_answer
    values = {"request_id": "queue", "choices": ["once", "session", "always", "deny"], **context}
    for choice in ("session", "always"):
        with pytest.raises(PermissionError):
            validate_answer("approval", values, {"choice": choice})
    assert validate_answer("approval", values, {"choice": "once"}) == {"choice": "once"}
    seen = []
    with pytest.raises(ValueError, match="Invalid server request params"):
        sr.send_async("approval", "runtime", values, seen.append, timeout=30)
    assert not sr.open_requests("runtime") and not seen


def test_invalid_internal_payment_never_publishes_credentials():
    payment = payment_context()
    payment["card"]["number"] = "DUMMY-PAN"
    with pytest.raises(ValueError) as error:
        sr.send_async("approval", "runtime", {"request_id": "queue", "payment": payment}, lambda result: None, timeout=30)
    assert "DUMMY-PAN" not in str(error.value)
    assert not sr.open_requests("runtime")


def test_deny_suggestion_reaches_queue_once_and_never_approves(isolated):
    published = threading.Event()
    def notify(data):
        server._emit_approval_request("runtime", data)
        published.set()
    with ThreadPoolExecutor(max_workers=3) as pool:
        future = pool.submit(_await_gateway_decision, "owner", notify,
                             {"command": "delete dummy", "allow_session": False,
                              "allow_permanent": False}, timeout_seconds=30)
        assert published.wait(5)
        request = sr.open_requests("runtime")[0]
        frame = {"id": request["id"], "result": {"choice": "deny", "reason": "Read only instead"}}
        results = list(pool.map(lambda _: sr.resolve_response(frame), range(2)))
        assert sorted(results) == [False, True]
        decision = future.result(timeout=5)
    assert decision == {"resolved": True, "choice": "deny", "reason": "Read only instead"}
    assert approval.list_gateway_approvals("owner") == []
    assert sr.open_requests("runtime") == []
    assert sr.resolve_response(frame) is False


def test_payment_extra_cannot_override_identity_scopes_or_deadline(monkeypatch, isolated):
    monkeypatch.setattr(approval_context, "get_current_session_key", lambda: "owner")
    monkeypatch.setattr(approval_context, "_is_gateway_approval_context", lambda: True)
    def notify(data):
        server._emit_approval_request("runtime", data)
        request = sr.open_requests("runtime")[0]
        params = request["params"]
        assert params["command"] == "Fill card at exact origin"
        assert params["description"] == "Approve this fill?"
        assert params["pattern_keys"] == ["mcp_elicitation"]
        assert params["choices"] == ["once", "deny"]
        assert params["kind"] == "payment"
        assert params["timeout_seconds"] == 30
        assert params["created_at"] != 1
        assert params["surface"] == "vault-payment"
        assert sr.resolve_response({"id": request["id"], "result": {"choice": "once"}})
    approval.register_gateway_notify("owner", notify)
    details = {}
    verdict = approval_prompt.request_elicitation_consent(
        "Fill card at exact origin", "Approve this fill?", surface="vault-payment", timeout_seconds=30,
        extra={"command": "not the action", "description": "not the reason", "pattern_keys": ["other"],
               "request_id": "forged", "allow_session": True, "allow_permanent": True,
               "choices": ["always"], "surface": "gateway", "created_at": 1, "expires_at": 2,
               "timeout_seconds": 1, "origin": "https://checkout.example"}, details=details)
    assert verdict == "accept"
    assert details["auth"] is None
    assert isinstance(details["decided_at"], float)


def payment_context():
    return {"v": 1, "origin": "https://checkout.example", "origin_bound": False,
            "merchant": {"name": "Example", "source": "page"},
            "total": {"amount_minor": 3277, "currency": "CAD", "display": "$32.77",
                      "source": "page", "label": "Total"},
            "lines": [{"name": "Dummy item", "qty": 1, "amount_minor": 2900}],
            "lines_truncated": False, "adjustments": [{"kind": "tax", "amount_minor": 377}],
            "card": {"label": "Dummy card", "last4": "1234", "backend": "bitwarden",
                     "limit": {"amount_minor": 3000, "currency": "CAD", "period": "per_purchase", "source": "config"}},
            "checks": {"total_found": True, "over_limit": True}}


def test_payment_v1_safe_snapshot_preserves_metadata(tmp_path):
    payment = payment_context()
    payload = server._approval_request_payload({"request_id": "queue", "surface": "vault-payment", "payment": payment})
    model = ApprovalRequestParams.model_validate({"session_id": "runtime", **payload})
    assert model.payment.model_dump(mode="json") == payment
    assert model.origin == payment["origin"]
    sr.send_async("approval", "runtime", payload, lambda result: None, timeout=30)
    safe = sr.scoped_pending("runtime", profile_home=str(tmp_path))[0]["params"]
    assert safe["payment"] == payment
    assert safe["kind"] == "payment"
    assert safe["choices"] == ["once", "deny"]


def test_payment_origin_conflict_is_not_an_alias():
    with pytest.raises(ValidationError):
        ApprovalRequestParams.model_validate({"session_id": "runtime", "request_id": "queue",
                                             "payment": payment_context(), "origin": "https://foreign.example"})


@pytest.mark.parametrize("over_limit", [True, False, None])
def test_real_payment_consent_preserves_warning_only_limit_and_auth(monkeypatch, over_limit):
    monkeypatch.setattr(approval_context, "get_current_session_key", lambda: "owner")
    monkeypatch.setattr(approval_context, "_is_gateway_approval_context", lambda: True)
    payment = payment_context()
    payment["checks"]["over_limit"] = over_limit
    def notify(data):
        server._emit_approval_request("runtime", data)
        request = sr.open_requests("runtime")[0]
        assert request["params"]["payment"] == payment
        assert request["params"]["choices"] == ["once", "deny"]
        assert sr.resolve_response({"id": request["id"], "result": {"choice": "once", "auth": "face_id"}})
    approval.register_gateway_notify("owner", notify)
    details = {}
    assert approval_prompt.request_elicitation_consent("Fill Dummy card", "Review exact origin",
        surface="vault-payment", extra={"payment": payment}, details=details, timeout_seconds=30) == "accept"
    assert details["auth"] == "face_id" and isinstance(details["decided_at"], float)
    assert not approval.list_gateway_approvals("owner") and not sr.open_requests("runtime")


@pytest.mark.parametrize("auth,expected", [("face_id", "face_id"), ("arbitrary-client-text", None)])
def test_pay_compatible_queue_api_keeps_auth_and_bounded_reason(auth, expected):
    from tools.approval_gateway_wait import _ApprovalEntry, _ApprovalNotice

    entry = _ApprovalEntry({"request_id": "queue", "surface": "vault-payment", "allow_session": False,
                            "allow_permanent": False}, timeout_seconds=30)
    approval._gateway_queues["owner"] = [entry]
    server._emit_approval_request("runtime", _ApprovalNotice(entry.data, entry.event.deadline_monotonic))
    request = sr.open_requests("runtime")[0]
    frame = {"id": request["id"], "result": {"choice": "deny", "reason": "Use another site", "auth": auth}}
    assert sr.resolve_response(frame)
    assert entry.result == "deny" and entry.reason == "Use another site"
    assert entry.auth == expected
    assert not sr.resolve_response(frame)
    assert entry.auth == expected


def test_payment_context_rejects_card_credentials():
    payment = {"v": 1, "origin": "https://checkout.example", "origin_bound": False,
               "card": {"label": "Dummy", "last4": "1234", "number": "DUMMY-PAN"},
               "checks": {"total_found": False, "over_limit": None}}
    with pytest.raises(ValidationError):
        ApprovalRequestParams.model_validate({"session_id": "runtime", "request_id": "queue",
                                             "choices": ["once", "deny"], "payment": payment})


@pytest.mark.parametrize("choice", ["session", "always"])
def test_bot_scope_remains_once_only_for_generic_approval(choice):
    sr.send_async("approval", "runtime", {"request_id": "queue", "choices": ["once", "session", "always", "deny"]},
                  lambda result: None, timeout=30)
    request = sr.open_requests("runtime")[0]
    with pytest.raises(PermissionError):
        sr.resolve_response({"id": request["id"], "result": {"choice": choice}},
                            expected_sid="runtime", expected_method="approval", mobile=True)
    assert sr.open_requests("runtime")


def test_bot_snapshot_offers_and_scope_fences_use_real_dispatch(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.transport import bind_transport, reset_transport

    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("root", "desktop")
        db._conn.execute("UPDATE sessions SET title = 'Bot Chat'")
    class Peer:
        def write(self, frame):
            return True
    peer = Peer()
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    server._sessions["runtime"] = {"session_key": "root", "profile_home": str(tmp_path),
                                   "transport": peer, "history_lock": threading.RLock(), "running": False}
    token = bind_transport(peer)
    try:
        seen = []
        sr.send_async("approval", "runtime", {"request_id": "queue",
                      "choices": ["once", "session", "always", "deny"]}, seen.append, timeout=30)
        request = sr.open_requests("runtime")[0]
        params = {"profile": "default", "canonical_root_id": "root", "session_id": "runtime"}
        def rpc(method, **extra):
            return server.handle_request({"id": "bot-test", "method": method, "params": {**params, **extra}})
        for choice in ("session", "always"):
            assert "error" in rpc("mobile.approval.respond", request_id=request["id"], choice=choice)
        assert "error" in rpc("mobile.approval.respond", canonical_root_id="foreign", request_id=request["id"], choice="once")
        assert seen == []
        snapshot = rpc("mobile.snapshot")["result"]
        assert snapshot["open_requests"][0]["params"]["choices"] == ["once", "deny"]
        assert sr.open_requests("runtime")[0]["params"]["choices"] == ["once", "session", "always", "deny"]
        assert rpc("mobile.approval.respond", request_id=request["id"], choice="deny")["result"]["resolved"] == 1
        assert seen == [{"choice": "deny"}]
    finally:
        reset_transport(token)
