"""Explicit server quick-consent projection; no sends or live state."""
import pytest
from tui_gateway.human_input import safe_params
from tui_gateway.mobile_push_attention import descriptor


def approval(**overrides):
    return {"request_id": "queue-fixture", "session_id": "runtime-fixture",
            "request_epoch": "epoch-fixture", "request_revision": 1,
            "choices": ["once", "deny"], "kind": "tool", "tool_name": "read_file",
            "risk": "read_only", **overrides}


def actions(params):
    return descriptor({"id": "srq-fixture", "method": "approval", "params": params})["allowed_actions"]


@pytest.mark.parametrize("extra", [
    {"risk": None}, {"risk": "unknown"}, {"risk": "destructive"}, {"risk": "sensitive"},
    {"risk": "future-risk"}, {"kind": "payment"}, {"kind": "terminal"},
    {"kind": "mcp_trust"}, {"kind": "future-kind"}, {"surface": "mcp-elicitation/fixture"},
    {"payment": {}}, {"destructive": True}, {"sensitive": True},
    {"destructive": "false"}, {"sensitive": None}, {"tool_name": "terminal"},
    {"choices": ["deny"]},
])
def test_risk_cannot_acquire_quick_once_before_or_after_projection(extra):
    params = approval(quick_once=True, **extra)
    safe = safe_params("approval", params)
    assert safe.get("quick_once") is False
    assert "once" not in actions(params)
    assert "once" not in actions(safe)


def test_legacy_missing_risk_and_claimed_eligibility_are_not_proof():
    params = approval(quick_once=True)
    del params["risk"]
    assert safe_params("approval", params).get("quick_once") is False
    assert "once" not in actions(params)


def test_explicit_read_only_preserves_positive_and_deny_suggest():
    params = approval(quick_once=False, destructive=False, sensitive=False)
    safe = safe_params("approval", params)
    assert safe["risk"] == "read_only"
    assert safe["quick_once"] is True
    assert set(actions(params)) == {"open", "once", "deny", "suggest"}
    assert actions(params) == actions(safe)


@pytest.mark.parametrize("extra,expected", [
    ({}, True), ({"risk": "unknown"}, False), ({"sensitive": True}, False),
    ({"destructive": True}, False), ({"kind": "payment"}, False),
])
def test_live_registry_frame_snapshot_and_safe_pending_agree(extra, expected):
    from tui_gateway import server_requests as sr
    frames = []
    sr.reset_for_tests()
    sr.bind_sinks(frames.append, lambda *args: None, lambda sid: True)
    try:
        sr.send_async("approval", "runtime-fixture", approval(quick_once=not expected, **extra), lambda result: None, timeout=300)
        snapshot = sr.open_requests("runtime-fixture")[0]
        assert snapshot["params"]["quick_once"] is expected
        assert frames[0]["params"]["quick_once"] is expected
        projected = safe_params("approval", snapshot["params"])
        assert projected["quick_once"] is expected
        assert ("once" in actions(projected)) is expected
    finally:
        sr.cancel()
        sr.reset_for_tests()


def test_private_extras_are_not_copied_to_quick_projection():
    safe = safe_params("approval", approval(private_extra="DUMMY_SECRET_SENTINEL"))
    assert "DUMMY_SECRET_SENTINEL" not in str(safe)
    assert "DUMMY_SECRET_SENTINEL" not in str(actions(safe))
