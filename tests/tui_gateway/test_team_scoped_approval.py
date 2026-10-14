"""Team adapters settle canonical requests, never cached/client-created authority."""
import threading
import time
from types import SimpleNamespace

import pytest

from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry
from tui_gateway import server, server_requests as sr
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC
from tui_gateway.hosted_room_service import HostedRoomService
from gateway import hosted_room_driver as driver
from tests.tui_gateway.test_methods_mobile import open_bot, scope, rpc

pytest_plugins = ["tests.tui_gateway.test_methods_mobile"]


@pytest.fixture
def bot(mobile_home, peer, monkeypatch):
    monkeypatch.setattr(approval, "_gateway_queues", {})
    snap = open_bot(profile="default", root="root")
    sr.bind_sinks(lambda frame: True, lambda *args: None, lambda sid: True)
    yield snap
    sr.cancel()
    sr.reset_for_tests()


def pending(bot, *, queue=False, payment=False):
    session = server._sessions[bot["session_id"]]
    data = {"request_id": "queue-exact", "choices": ["once", "deny"]}
    if payment:
        data["kind"] = "payment"
    if queue:
        entry = _ApprovalEntry(data, timeout_seconds=300)
        approval._gateway_queues[session["session_key"]] = [entry]
        server._emit_approval_request(bot["session_id"], entry.data)
        return entry
    seen = []
    sr.send_async("approval", bot["session_id"], data, seen.append, timeout=300)
    return seen


def row(bot):
    return rpc("mobile.snapshot", **scope(bot))["result"]["pending_approvals"][0]


def enhanced(row):
    return {k: row[k] for k in ("scoped_approval_response", "request_epoch", "request_revision")}


@pytest.mark.parametrize("queue", [False, True])
def test_bot_canonical_envelope_and_reason_delivery(bot, queue):
    target = pending(bot, queue=queue)
    current = row(bot)
    assert current["scoped_approval_response"] == 1
    assert current["expires_at"] > time.time()
    assert rpc("mobile.capabilities")["result"]["feature_versions"]["scoped_approval_response"] == 1
    result = rpc("mobile.approval.respond", **scope(bot), request_id=current["request_id"],
                 choice="deny", reason="Use a read-only inspection instead", auth="face_id", **enhanced(current))
    assert result["result"]["resolved"] == 1, result
    if queue:
        assert target.result == "deny" and target.reason == "Use a read-only inspection instead"
        assert target.auth == "face_id"
    else:
        assert target == [{"choice": "deny", "reason": "Use a read-only inspection instead", "auth": "face_id"}]
    assert not sr.open_requests(bot["session_id"])
    assert rpc("mobile.approval.respond", **scope(bot), request_id=current["request_id"],
               choice="deny", **enhanced(current))["result"]["resolved"] == 0


@pytest.mark.parametrize("queue", [False, True])
@pytest.mark.parametrize("mutation", ["epoch", "revision", "scope", "reason", "missing", "wrong-id"])
def test_bot_rejects_stale_or_invalid_enhanced_without_consumption(bot, queue, mutation):
    target = pending(bot, queue=queue)
    current = row(bot)
    response = {**scope(bot), "request_id": current["request_id"], "choice": "once", **enhanced(current)}
    if mutation == "epoch": response["request_epoch"] = "old-process"
    if mutation == "revision": response["request_revision"] += 1
    if mutation == "scope": response["choice"] = "always"
    if mutation == "reason": response["reason"] = "This cannot approve an unseen alternative"
    if mutation == "missing": response.pop("request_epoch")
    if mutation == "wrong-id": response["request_id"] = "absent"
    result = rpc("mobile.approval.respond", **response)
    assert "error" in result or result.get("result", {}).get("resolved") == 0
    assert sr.open_requests(bot["session_id"])
    assert (target.result is None) if queue else (target == [])


@pytest.mark.parametrize("queue", [False, True])
def test_bot_legacy_payment_is_deny_only_enhanced_needs_recognized_auth(bot, queue):
    target = pending(bot, queue=queue, payment=True)
    current = row(bot)
    for fields in ({}, enhanced(current), {**enhanced(current), "auth": "invented"}):
        result = rpc("mobile.approval.respond", **scope(bot), request_id=current["request_id"], choice="once", **fields)
        assert "error" in result or result.get("result", {}).get("resolved") == 0
        assert sr.open_requests(bot["session_id"])
    accepted = rpc("mobile.approval.respond", **scope(bot), request_id=current["request_id"],
                   choice="once", auth="passcode", **enhanced(current))
    assert accepted["result"]["resolved"] == 1
    assert target.result == "once" if queue else target == [{"choice": "once", "auth": "passcode"}]


def test_legacy_plain_once_and_deny_remain_available(bot):
    seen = pending(bot)
    result = rpc("mobile.approval.respond", **scope(bot), request_id=row(bot)["request_id"], choice="once")
    assert result["result"]["resolved"] == 1
    assert seen == [{"choice": "once"}]


def test_queue_creation_canonical_envelope_and_atomic_scope(monkeypatch):
    monkeypatch.setattr(approval, "_gateway_queues", {})
    entry = _ApprovalEntry({"choices": ["once", "deny"]}, timeout_seconds=300)
    approval._gateway_queues["owner"] = [entry]
    assert entry.data["scoped_approval_response"] == 1
    for epoch, revision in [("old", 1), (entry.data["request_epoch"], 2)]:
        assert approval.resolve_gateway_approval("owner", "once", request_id=entry.data["request_id"],
            expected_epoch=epoch, expected_revision=revision) == 0
        assert entry.result is None
    assert approval.resolve_gateway_approval("owner", "deny", reason="read only", request_id=entry.data["request_id"],
        expected_epoch=entry.data["request_epoch"], expected_revision=entry.data["request_revision"]) == 1


def room(tmp_path, monkeypatch, receipt=None):
    service = HostedRoomService(SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock()),
                                db_path=tmp_path / "room.db")
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(room_id="room", name="Review", members=[
        {"member_id": "default", "profile": "default", "handle": "hermes"},
        {"member_id": "ops", "profile": "ops", "handle": "ops"}])
    service.send(room_id="room", event_id="input", payload={"text": "@ops inspect", "thread_id": "thread"})
    task = driver.list_tasks(service.db_path, room_id="room", status="queued")[0]
    binding = service.bindings()[0]
    lease = driver.acquire_lease(service.db_path, room_id="room", gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch, process_generation="test", ttl_seconds=30, clock=time.time)
    driver.start_task(service.db_path, task["identity"], lease, expected_cancel_generation=0, clock=time.time)
    task = driver.get_task(service.db_path, task["identity"])
    envelope = {"request_id": "exact", "choices": ["once", "deny"], "scoped_approval_response": 1,
                "request_epoch": "canonical", "request_revision": 1, "expires_at": time.time() + 300}
    service.runtime._report_pending_action(task, session_id="hidden", info={"pending_approval": envelope})
    calls = []
    def approve(**kwargs):
        calls.append(kwargs)
        return {"resolved": 1} if receipt is None else receipt
    service.rpc = SimpleNamespace(approve=approve)
    params = {"member_id": "ops", "task_id": task["identity"].task_id,
        "execution_generation": task["execution_generation"], "choice": "deny", "request_id": "exact",
        **enhanced(envelope), "expected_authority_gateway_id": binding.gateway_id,
        "expected_authority_epoch": binding.authority_epoch, "reason": "read only", "auth": "face_id"}
    return service, task, params, calls


@pytest.mark.parametrize("receipt", [{"resolved": 0}, {"status": "unknown"}, {"status": "outcome_unknown"}])
def test_room_unconfirmed_receipt_retains_pending(tmp_path, monkeypatch, receipt):
    service, _task, params, calls = room(tmp_path, monkeypatch, receipt)
    service.approve_room_task("room", **params)
    assert len(service.status("room")["pending_actions"]) == 1
    assert calls[0]["reason"] == "read only"
    assert calls[0]["request_epoch"] == "canonical"


@pytest.mark.parametrize("mutation", ["authority", "generation", "epoch", "revision", "absent-capability", "settled-task"])
def test_room_current_authority_required_before_forward(tmp_path, monkeypatch, mutation):
    service, _task, params, calls = room(tmp_path, monkeypatch)
    if mutation == "authority": params["expected_authority_epoch"] += 1
    if mutation == "generation": params["execution_generation"] += 1
    if mutation == "epoch": params["request_epoch"] = "old"
    if mutation == "revision": params["request_revision"] += 1
    if mutation == "absent-capability": service._pending_actions[("room", "ops")]["approval"].pop("scoped_approval_response")
    if mutation == "settled-task":
        import sqlite3
        with sqlite3.connect(service.db_path) as conn:
            conn.execute("UPDATE hosted_room_driver_tasks SET status='settled'")
    with pytest.raises((RuntimeError, ValueError, PermissionError)):
        service.approve_room_task("room", **params)
    assert calls == []
    assert service.status("room")["pending_actions"]


def test_room_local_enhanced_forward_and_authoritative_clear(tmp_path, monkeypatch):
    service, _task, params, calls = room(tmp_path, monkeypatch)
    assert service.approve_room_task("room", **params) == {"resolved": 1}
    assert calls[0]["scoped_approval_response"] == 1
    assert not service.status("room")["pending_actions"]


def test_local_rpc_enhanced_never_uses_stale_legacy_fallback(bot):
    target = pending(bot, queue=True)
    current = row(bot)
    client = HostedRoomServerRPC(server)
    server._sessions[bot["session_id"]]["_hosted_room_task"] = {"task_id": "task", "execution_generation": 1}
    with pytest.raises(RuntimeError):
        client.approve(session_id=bot["session_id"], request_id=current["request_id"], choice="deny",
                       reason="read only", task_id="task", execution_generation=1, **enhanced(current))
    assert target.result is None
    assert sr.open_requests(bot["session_id"])
    with pytest.raises((RuntimeError, ValueError)):
        client.approve(session_id="missing-runtime", request_id=current["request_id"], choice="deny", **enhanced(current))


def test_driver_never_invents_offered_choices(tmp_path, monkeypatch):
    service, task, _params, _calls = room(tmp_path, monkeypatch)
    service.runtime._report_pending_action(task, session_id="hidden", info={"pending_approval": {"request_id": "exact", "choices": []}})
    assert service.status("room")["pending_actions"][0]["approval"]["choices"] == []
