"""Frozen Pay queue deltas retain U1 authority, timing and first settlement."""
import pytest

from tools import approval
from tools import approval_gateway_wait as wait


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(approval, "_gateway_notify_cbs", {})


def entry(request_id, **data):
    return wait._ApprovalEntry({"request_id": request_id, **data}, timeout_seconds=30)


@pytest.mark.parametrize("data", [
    {"surface": "vault-payment"}, {"kind": "payment"}, {"payment": {"v": 1}},
    {"surface": "mcp-trust/dummy"}, {"surface": "mcp-elicitation/dummy"},
    {"kind": "mcp_trust"}, {"kind": "mcp_elicitation"},
])
def test_unnormalized_queue_consent_cannot_grant_reusable_scope(data):
    request = entry("consent", choices=["once", "session", "always", "deny"], **data)
    approval._gateway_queues["owner"] = [request]
    for choice in ("session", "always"):
        assert approval.resolve_gateway_approval("owner", choice, request_id="consent") == 0
        assert request.result is None and not request.event.is_set()
    assert request.data["choices"] == ["once", "deny"]
    assert approval.resolve_gateway_approval("owner", "once", request_id="consent") == 1


@pytest.mark.parametrize("data", [{"surface": "vault-payment"}, {"kind": "payment"}, {"payment": {"v": 1}}])
@pytest.mark.parametrize("choice", ["once", "session", "always"])
def test_bulk_approve_excludes_payment_but_exact_id_and_bulk_deny_remain(data, choice):
    payment, ordinary = entry("payment", **data), entry("ordinary")
    approval._gateway_queues["owner"] = [payment, ordinary]
    assert approval.resolve_gateway_approval("owner", choice, resolve_all=True) == 1
    assert ordinary.result == choice and payment.result is None
    assert approval.resolve_gateway_approval("owner", "once", resolve_all=True) == 0
    assert approval.resolve_gateway_approval("owner", "once", request_id="foreign") == 0
    assert approval.resolve_gateway_approval("owner", "once", request_id="payment", auth="face_id") == 1
    assert payment.auth == "face_id"
    assert approval.resolve_gateway_approval("owner", "deny", request_id="payment", auth="passcode") == 0
    assert payment.result == "once" and payment.auth == "face_id"
    payment, ordinary = entry("payment", **data), entry("ordinary")
    approval._gateway_queues["owner"] = [payment, ordinary]
    assert approval.resolve_gateway_approval("owner", "deny", resolve_all=True, reason="Review separately") == 2
    assert payment.reason == ordinary.reason == "Review separately"


def test_expired_or_invalid_targets_are_not_popped(monkeypatch):
    payment = entry("payment", surface="vault-payment")
    ordinary = entry("ordinary")
    approval._gateway_queues["owner"] = [payment, ordinary]
    for kwargs in ({"reason": "not a denial"}, {"request_id": "foreign"}):
        assert approval.resolve_gateway_approval("owner", "once", **kwargs) == 0
    monkeypatch.setattr(wait.time, "monotonic", lambda: payment.event.deadline_monotonic)
    assert approval.resolve_gateway_approval("owner", "once", request_id="payment", auth="face_id") == 0
    assert approval._gateway_queues["owner"] == [payment, ordinary]
    assert payment.result is None and not payment.event.is_set()


@pytest.mark.parametrize("auth,expected", [("face_id", "face_id"), ("touch_id", "touch_id"),
    ("optic_id", "optic_id"), ("passcode", "passcode"), ("client prose", None), ({"secret": "DUMMY"}, None)])
def test_real_wait_returns_only_normalized_answer_metadata(auth, expected):
    def notify(data):
        assert approval.resolve_gateway_approval("owner", "once", request_id=data["request_id"], auth=auth) == 1
    decision = wait._await_gateway_decision("owner", notify, {}, surface="vault-payment", timeout_seconds=30)
    assert decision == {"resolved": True, "choice": "once", "reason": None, **({"auth": expected} if expected else {})}


def test_legacy_ordinary_scopes_and_frozen_notice_remain():
    ordinary = entry("ordinary")
    approval._gateway_queues["owner"] = [ordinary]
    assert approval.resolve_gateway_approval("owner", "always", request_id="ordinary") == 1
    assert ordinary.result == "always"
    def notify(data):
        request = approval._gateway_queues["owner"][0]
        assert data.deadline_monotonic == request.event.deadline_monotonic
        assert data["expires_at"] - data["created_at"] == 30
        assert approval.resolve_gateway_approval("owner", "deny", request_id=data["request_id"]) == 1
    decision = wait._await_gateway_decision("owner", notify, {}, timeout_seconds=30)
    assert decision == {"resolved": True, "choice": "deny", "reason": None}


@pytest.mark.parametrize("surface", ["vault-payment", "mcp-elicitation/example"])
def test_real_consent_wait_keeps_requester_binding_and_deny_only_card_cutoff(monkeypatch, surface):
    monkeypatch.setattr(wait._ctx, "_get_approval_config", lambda: {"approvers": []})
    actor = {"HERMES_SESSION_ACTOR_ID": "alice", "HERMES_SESSION_ACTOR_NAME": "Alice",
             "HERMES_SESSION_CHAT_TYPE": "group"}
    monkeypatch.setattr(wait._ctx, "_session_env", lambda key: actor.get(key, ""))

    def notify(data):
        request = approval._gateway_queues["owner"][0]
        rid = data["request_id"]
        assert request.card_deny_only is True
        assert request.authority["requester_id"] == "alice" and request.authority["approvers"] == []
        assert "authority" not in data
        assert approval.resolve_gateway_approval_card("owner", "once", rid, actor_id="alice") == 0
        approval.restrict_gateway_approval_card_to_deny("owner", rid, False)
        assert approval.resolve_gateway_approval_card("owner", "once", rid, actor_id="bob") == 0
        for choice in ("session", "always"):
            assert approval.resolve_gateway_approval_card("owner", choice, rid, actor_id="alice") == 0
        assert approval.resolve_gateway_approval("owner", "once", request_id=rid,
            expected_epoch="stale", expected_revision=data["request_revision"]) == 0
        assert approval.resolve_gateway_approval("owner", "once", request_id=rid,
            expected_epoch=data["request_epoch"], expected_revision=data["request_revision"], auth="face_id") == 1

    decision = wait._await_gateway_decision("owner", notify, {}, surface=surface, timeout_seconds=30)
    assert decision == {"resolved": True, "choice": "once", "reason": None, "auth": "face_id"}


def test_identical_payments_need_separate_live_decisions(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    published = threading.Event()
    notices = []
    lock = threading.Lock()
    def notify(data):
        with lock:
            notices.append(data)
            if len(notices) == 2:
                published.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(wait._await_gateway_decision, "owner", notify,
            {"command": "Same dummy checkout", "pattern_keys": ["same"]},
            surface="vault-payment", timeout_seconds=30) for _ in range(2)]
        try:
            assert published.wait(5), "Payments must not coalesce into a leader's decision"
            ids = [notice["request_id"] for notice in notices]
            assert ids[0] != ids[1]
            assert approval.resolve_gateway_approval("owner", "once", resolve_all=True) == 0
            assert approval.resolve_gateway_approval("owner", "once", request_id=ids[1]) == 1
            assert [item["request_id"] for item in approval.list_gateway_approvals("owner")] == [ids[0]]
            assert approval.resolve_gateway_approval("owner", "deny", request_id=ids[0]) == 1
            assert sorted(future.result(5)["choice"] for future in futures) == ["deny", "once"]
        finally:
            approval.unregister_gateway_notify("owner")
