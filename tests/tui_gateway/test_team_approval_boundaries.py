"""Additional Team boundaries: real queue-only authority and actual service->local resolver."""
import copy
import time
from types import SimpleNamespace

import pytest

from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry
from tui_gateway import server, server_requests as sr
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC
from tests.tui_gateway.test_team_scoped_approval import pending, row, enhanced, scope, rpc, room

pytest_plugins = ["tests.tui_gateway.test_team_scoped_approval"]


@pytest.mark.parametrize("mutation", [None, "unsupported", "expired", "boolean-revision", "boolean-capability", "reason-too-long", "runtime-replaced"])
def test_queue_only_bot_exact_canonical_creation(bot, mutation):
    record = server._sessions[bot["session_id"]]
    entry = _ApprovalEntry({"request_id": "queue-only", "choices": ["once", "deny"]}, timeout_seconds=300)
    approval._gateway_queues[record["session_key"]] = [entry]
    if mutation == "unsupported": entry.data.pop("scoped_approval_response")
    row(bot)
    fields = {"scoped_approval_response": 1, "request_epoch": entry.data["request_epoch"], "request_revision": 1}
    if mutation == "expired": entry.event.deadline_monotonic = time.monotonic() - 1
    if mutation == "boolean-revision": fields["request_revision"] = True
    if mutation == "boolean-capability": fields["scoped_approval_response"] = True
    if mutation == "reason-too-long": fields["reason"] = "x" * 2001
    if mutation == "runtime-replaced": server._sessions[bot["session_id"]] = {**record, "session_key": "different"}
    response = rpc("mobile.approval.respond", **scope(bot), request_id="queue-only", choice="deny", **fields)
    if mutation is None:
        assert response["result"]["resolved"] == 1
        assert entry.result == "deny"
    else:
        assert "error" in response or response.get("result", {}).get("resolved") == 0
        assert entry.result is None
    assert not sr.open_requests(bot["session_id"])


@pytest.mark.parametrize("payment", [False, True])
def test_legacy_deny_with_no_metadata_is_preserved(bot, payment):
    target = pending(bot, payment=payment)
    result = rpc("mobile.approval.respond", **scope(bot), request_id=row(bot)["request_id"], choice="deny")
    assert result["result"]["resolved"] == 1
    assert target == [{"choice": "deny"}]


@pytest.mark.parametrize("mutation", [None, "runtime-generation", "authority-at-consumer"])
def test_actual_service_local_consumer_checks_current_authority(bot, tmp_path, monkeypatch, mutation):
    entry = pending(bot, queue=True)
    canonical = row(bot)
    service, task, params, _calls = room(tmp_path, monkeypatch)
    record = server._sessions[bot["session_id"]]
    record["_hosted_room_task"] = {"room_id": "room", "task_id": task["identity"].task_id,
        "execution_generation": task["execution_generation"], "member_id": "ops"}
    service._pending_actions[("room", "ops")].update(session_id=bot["session_id"],
        request_id=canonical["request_id"], approval=canonical)
    params.update(request_id=canonical["request_id"], **enhanced(canonical))
    client = HostedRoomServerRPC(server)
    if mutation == "runtime-generation": record["_hosted_room_task"]["execution_generation"] += 1
    if mutation == "authority-at-consumer":
        original = client.approve
        def changed_authority(**kwargs):
            import sqlite3
            with sqlite3.connect(service.db_path) as conn:
                conn.execute("UPDATE hosted_rooms SET authority_epoch=authority_epoch+1")
            return original(**kwargs)
        service.rpc = SimpleNamespace(approve=changed_authority)
    else:
        service.rpc = client
    if mutation is None:
        assert service.approve_room_task("room", **params)["resolved"] == 1
        assert entry.reason == "read only" and entry.auth == "face_id"
        assert not sr.open_requests(bot["session_id"])
        assert not service.status("room")["pending_actions"]
    else:
        with pytest.raises((RuntimeError, PermissionError, ValueError)):
            service.approve_room_task("room", **params)
        assert entry.result is None
        assert sr.open_requests(bot["session_id"])
        assert service.status("room")["pending_actions"]


def test_accepted_old_receipt_cannot_clear_new_envelope(tmp_path, monkeypatch):
    service, _task, params, _calls = room(tmp_path, monkeypatch)
    def replace_pending(**kwargs):
        new = copy.deepcopy(service._pending_actions[("room", "ops")])
        new["approval"]["request_revision"] += 1
        service._pending_actions[("room", "ops")] = new
        return {"resolved": 1}
    service.rpc = SimpleNamespace(approve=replace_pending)
    service.approve_room_task("room", **params)
    assert service.status("room")["pending_actions"][0]["approval"]["request_revision"] == 2


@pytest.mark.parametrize("receipt", [{"resolved": 1}, {"resolved": 0}, {"resolved": 1, "status": "outcome_unknown"}])
def test_actual_groups_handler_retains_metadata_and_honest_receipt(tmp_path, monkeypatch, receipt):
    service, _task, params, calls = room(tmp_path, monkeypatch, receipt)
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: service)
    result = rpc("groups.approve", room_id="room", **params)
    assert "result" in result, result
    accepted = receipt == {"resolved": 1}
    assert result["result"]["approved"] is accepted
    assert bool(service.status("room")["pending_actions"]) is not accepted
    assert calls[0]["reason"] == "read only"
    assert calls[0]["auth"] == "face_id"
    assert calls[0]["expected_authority_epoch"] == params["expected_authority_epoch"]


def test_compute_child_mirror_does_not_advertise_local_enhanced_authority(bot):
    envelope = {"request_id": "remote-queue", "choices": ["once", "deny"], "scoped_approval_response": 1,
        "request_epoch": "child-process", "request_revision": 1, "expires_at": time.time() + 300}
    server._sessions[bot["session_id"]]["_compute_host_open_requests"] = {
        "srq-remote": {"id": "srq-remote", "method": "approval", "params": envelope}}
    snapshot = rpc("mobile.snapshot", **scope(bot))["result"]
    assert "scoped_approval_response" not in snapshot["pending_approvals"][0]
    assert "scoped_approval_response" not in snapshot["open_requests"][0]["params"]
    result = rpc("mobile.approval.respond", **scope(bot), request_id="srq-remote", choice="once", **enhanced(envelope))
    assert "error" in result or result["result"]["resolved"] == 0


def test_local_authority_fence_is_held_at_exact_registry_settlement(bot, tmp_path, monkeypatch):
    entry = pending(bot, queue=True)
    canonical = row(bot)
    service, task, params, _calls = room(tmp_path, monkeypatch)
    server._sessions[bot["session_id"]]["_hosted_room_task"] = {
        "room_id": "room", "member_id": "ops", "task_id": task["identity"].task_id,
        "execution_generation": task["execution_generation"]}
    service._pending_actions[("room", "ops")].update(session_id=bot["session_id"],
        request_id=canonical["request_id"], approval=canonical)
    params.update(request_id=canonical["request_id"], **enhanced(canonical))
    service.rpc = HostedRoomServerRPC(server)
    original = sr.scoped_respond
    checked = []
    def fenced_response(*args, **kwargs):
        import sqlite3
        with sqlite3.connect(service.db_path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE hosted_rooms SET authority_epoch=authority_epoch+1")
        checked.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(sr, "scoped_respond", fenced_response)
    assert service.approve_room_task("room", **params)["resolved"] == 1
    assert checked == [True]
    assert entry.result == "deny"


def test_peer_home_authority_fence_is_held_through_dispatch(tmp_path, monkeypatch):
    service, _task, params, _calls = room(tmp_path, monkeypatch)
    checked = []
    def fenced_peer(**kwargs):
        import sqlite3
        with sqlite3.connect(service.db_path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE hosted_rooms SET authority_epoch=authority_epoch+1")
        checked.append(kwargs)
        return {"resolved": 1}
    service.peer_routes[("room", "ops")] = SimpleNamespace(grant="signed.room.grant")
    service.peer_clients[("room", "ops")] = SimpleNamespace(approve_receipt=fenced_peer)
    assert service.approve_room_task("room", **params)["resolved"] == 1
    assert checked[0]["expected_authority_epoch"] == params["expected_authority_epoch"]
    assert not service.status("room")["pending_actions"]
