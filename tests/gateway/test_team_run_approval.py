"""Actual target API handler consumes enhanced queue fields under existing run/grant fences."""
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.gateway.test_api_server_runs import _create_runs_app

from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry

pytest_plugins = ["tests.gateway.test_api_server_runs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "epoch", "revision", "authority", "reason-on-once", "missing-envelope", "wide-choice", "payment-no-auth", "payment-legacy", "payment-valid", "oversized-reason"])
async def test_actual_run_approval_metadata_and_authority(auth_adapter, monkeypatch, mutation):
    monkeypatch.setattr(approval, "_gateway_queues", {})
    run_id = "exact-run"
    data = {"request_id": "exact-approval", "choices": ["once", "deny"]}
    if mutation and mutation.startswith("payment"):
        data["kind"] = "payment"
    entry = _ApprovalEntry(data, timeout_seconds=300)
    approval._gateway_queues[run_id] = [entry]
    auth_adapter._run_approval_sessions[run_id] = run_id
    auth_adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_approval", "approval": dict(entry.data)}
    body = {"choice": "deny", "request_id": data["request_id"], "reason": "Use read-only instead", "auth": "face_id",
        **{k: entry.data[k] for k in ("scoped_approval_response", "request_epoch", "request_revision")},
        "expected_authority_gateway_id": "home-gateway", "expected_authority_epoch": 1}
    if mutation == "epoch": body["request_epoch"] = "previous-process"
    if mutation == "revision": body["request_revision"] += 1
    if mutation == "authority": body["expected_authority_epoch"] += 1
    if mutation == "reason-on-once": body["choice"] = "once"
    if mutation == "missing-envelope": body.pop("request_epoch")
    if mutation == "wide-choice": body["choice"] = "always"; body.pop("reason")
    if mutation == "oversized-reason": body["reason"] = "x" * 2001
    if mutation and mutation.startswith("payment"):
        body["choice"] = "once"
        body.pop("reason")
        if mutation == "payment-no-auth": body["auth"] = "not-recognized"
        if mutation == "payment-legacy": body = {"choice": "once", "request_id": data["request_id"]}
    with patch.object(auth_adapter, "_check_run_auth", return_value=None), \
         patch.object(auth_adapter, "_request_owns_run", return_value=True), \
         patch.object(auth_adapter, "_room_grant_token", return_value="existing-scope-grant"), \
         patch.object(auth_adapter, "_room_grant_claims", return_value={"authority_gateway_id": "home-gateway", "authority_epoch": 1}):
        async with TestClient(TestServer(_create_runs_app(auth_adapter))) as client:
            response = await client.post(f"/v1/runs/{run_id}/approval", json=body)
            result = await response.json()
    if mutation in (None, "payment-valid"):
        assert response.status == 200, result
        assert result["resolved"] == 1
        assert entry.result == body["choice"]
        assert entry.auth == "face_id"
        assert entry.reason == body.get("reason")
        # Private suggestions are not persisted in run status or event receipts.
        assert "Use read-only instead" not in str(auth_adapter._run_statuses[run_id])
    else:
        assert response.status in (400, 403, 409), result
        assert entry.result is None
        assert approval.list_gateway_approvals(run_id)


@pytest.mark.asyncio
async def test_enhanced_does_not_bypass_existing_auth(auth_adapter, monkeypatch):
    entry = _ApprovalEntry({"request_id": "exact", "choices": ["once", "deny"]}, timeout_seconds=300)
    monkeypatch.setattr(approval, "_gateway_queues", {"run": [entry]})
    async with TestClient(TestServer(_create_runs_app(auth_adapter))) as client:
        response = await client.post("/v1/runs/run/approval", json={"choice": "once", "auth": "face_id",
            **{k: entry.data[k] for k in ("scoped_approval_response", "request_epoch", "request_revision")}})
    assert response.status == 401
    assert entry.result is None
