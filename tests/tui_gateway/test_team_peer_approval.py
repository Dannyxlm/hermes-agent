"""Enhanced fields survive the actual peer HTTP transport, not a method stub."""
import time

import pytest

from tests.tui_gateway.test_hosted_room_peer_http import FakePeer, _dispatch

from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient, PeerRunsHTTPError

pytest_plugins = ["tests.tui_gateway.test_hosted_room_peer_http"]


@pytest.mark.parametrize("mutation", [None, "absent-capability", "epoch", "revision", "authority", "generation", "reason-on-once"])
def test_actual_peer_http_exact_forwarding(peer_server, mutation):
    client = PeerRunsHTTPClient(base_url=peer_server, api_key="")
    client.dispatch(dispatch=_dispatch(), grant="signed.room.grant")
    envelope = {"request_id": "exact", "choices": ["once", "deny"], "scoped_approval_response": 1,
        "request_epoch": "canonical", "request_revision": 1, "expires_at": time.time() + 300}
    FakePeer.runs["run-1"].update(status="waiting_for_approval", approval=dict(envelope))
    body = {"task_id": "task-1", "execution_generation": 1, "request_id": "exact", "choice": "deny",
        "reason": "Read-only instead", "auth": "touch_id", "grant": "signed.room.grant",
        **{k: envelope[k] for k in ("scoped_approval_response", "request_epoch", "request_revision")},
        "expected_authority_gateway_id": "gateway-home", "expected_authority_epoch": 1}
    if mutation == "absent-capability": FakePeer.runs["run-1"]["approval"].pop("scoped_approval_response")
    if mutation == "epoch": body["request_epoch"] = "old"
    if mutation == "revision": body["request_revision"] += 1
    if mutation == "authority": body["expected_authority_epoch"] += 1
    if mutation == "generation": body["execution_generation"] += 1
    if mutation == "reason-on-once": body["choice"] = "once"
    if mutation == "generation":
        assert client.approve_receipt(**body) is None
    elif mutation is not None:
        with pytest.raises((PeerRunsHTTPError, ValueError, PermissionError)):
            client.approve_receipt(**body)
    else:
        result = client.approve_receipt(**body)
        assert result["resolved"] == 1
        expected = {k: v for k, v in body.items() if k not in {"grant", "task_id", "execution_generation"}}
        assert FakePeer.approvals == [expected]
    if mutation is not None:
        assert FakePeer.approvals == []
