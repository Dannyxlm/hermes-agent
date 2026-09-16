"""Native authorization and detached event delivery exercise the real dispatcher."""

import json
import uuid

import pytest

from tests.tui_gateway.test_methods_mobile import mobile_home, peer, open_bot, rpc, scope
from tests.tui_gateway.test_mobile_push import Sender
from tui_gateway import server
from tui_gateway.mobile_push import PushService


@pytest.fixture
def push_service(mobile_home, monkeypatch):
    service = PushService(mobile_home / "mobile-push" / "outbox.sqlite3", Sender())
    monkeypatch.setattr(server, "_mobile_push_service", lambda: service)
    yield service
    service.close()


def registration(opened):
    return {**scope(opened), "installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4()),
            "device_token": "ab" * 32, "environment": "production"}


def test_auth_profile_and_transport_scopes_cannot_be_spoofed(push_service, peer):
    opened = open_bot()
    params = registration(opened)
    assert rpc("mobile.push.register", **params)["error"]["code"] == 4403
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    assert rpc("mobile.push.register", **{**params, "profile": "default"})["error"]["code"] == 4400
    assert "result" in rpc("mobile.push.register", **params)
    session = server._sessions[opened["session_id"]]
    session["transport"] = server._detached_ws_transport
    assert rpc("mobile.push.register", **params)["error"]["code"] == 4400
    server._sessions.clear()
    peer.auth_identity = {"user_id": "foreign-user", "provider": "test-auth"}
    ids = {key: params[key] for key in ("installation_id", "connection_id")}
    assert rpc("mobile.push.unregister", **ids)["result"]["removed"] == 0
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    assert rpc("mobile.push.unregister", **ids)["result"]["removed"] == 1


def test_completion_is_captured_before_detached_transport_drops_it(push_service, peer):
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    opened = open_bot()
    params = registration(opened)
    assert "result" in rpc("mobile.push.register", **params)
    sid = opened["session_id"]
    server._emit("message.start", sid)
    snapshot = rpc("mobile.snapshot", **scope(opened))["result"]
    run_id = snapshot["notification_run"]["run_id"]
    session = server._sessions[sid]
    session["transport"] = server._detached_ws_transport
    frame = server._event_frame("message.complete", sid, {"status": "complete", "text": "private transcript sentinel"})
    assert server.write_json(frame) is False
    server.write_json(frame)
    push_service.drain_once()
    assert len(push_service.sender.jobs) == 1
    job = push_service.sender.jobs[0]
    assert "private transcript sentinel" not in json.dumps(job["payload"])
    assert job["payload"]["hermex.destination"]["run_id"] == run_id
    assert job["payload"]["hermex.destination"]["canonical_root_id"] == "ops-root"


def test_activity_registration_requires_current_valid_scope_and_known_run(push_service, peer):
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    opened = open_bot()
    params = registration(opened)
    params.pop("device_token")
    params.update(activity_token="cd" * 32, activity_id="activity", run_id="missing")
    assert "error" in rpc("mobile.activity.register", **params)
    server._emit("message.start", opened["session_id"])
    run = rpc("mobile.snapshot", **scope(opened))["result"]["notification_run"]
    params["run_id"] = run["run_id"]
    assert "result" in rpc("mobile.activity.register", **params)
    server._mobile_push_finish(server._sessions[opened["session_id"]], "interrupted")
    push_service.drain_once()
    assert push_service.sender.jobs[-1]["payload"]["aps"]["event"] == "end"
    assert push_service.sender.jobs[-1]["payload"]["aps"]["content-state"]["status"] == "cancelled"


def test_projection_storage_failure_does_not_drop_canonical_frame(push_service, peer, monkeypatch):
    import sqlite3
    opened = open_bot()
    def unavailable():
        raise sqlite3.DatabaseError("private storage failure sentinel")
    monkeypatch.setattr(server, "_mobile_push_service", unavailable)
    frame = server._event_frame("message.start", opened["session_id"])
    assert server.write_json(frame) is True
    assert peer.frames[-1] is frame
    assert rpc("mobile.snapshot", **scope(opened))["result"]["notification_run"] is None


def test_refresh_needs_auth_but_no_attached_runtime(push_service, peer):
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    opened = open_bot()
    params = registration(opened)
    assert "result" in rpc("mobile.push.register", **params)
    server._sessions.clear()
    refresh = {key: params[key] for key in ("installation_id", "connection_id", "environment")}
    refresh.update(device_token="ef" * 32, categories=["completion"])
    peer.auth_identity = {"user_id": "other-user", "provider": "test-auth"}
    assert rpc("mobile.push.refresh", **refresh)["result"]["updated"] == 0
    peer.auth_identity = None
    assert rpc("mobile.push.refresh", **refresh)["error"]["code"] == 4403
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    assert rpc("mobile.push.refresh", **refresh)["result"]["updated"] == 1
    assert not server._sessions
    assert rpc("mobile.push.unregister", **{key: refresh[key] for key in ("installation_id", "connection_id")})["result"]["removed"] == 1
    assert rpc("mobile.push.refresh", **refresh)["result"]["updated"] == 0


def test_native_completion_preview_uses_final_text_only_when_opted_in(push_service, peer):
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    opened = open_bot()
    assert "result" in rpc("mobile.push.register", **registration(opened), preview_enabled=True)
    sid = opened["session_id"]
    server._emit("message.start", sid)
    server._emit("message.complete", sid, {"status": "complete", "text": "**Bot reply** is ready", "reasoning": "hidden"})
    push_service.drain_once()
    alert = push_service.sender.jobs[0]["payload"]["aps"]["alert"]
    assert alert["body"] == "Bot reply is ready"
    assert alert["title"]
    assert "hidden" not in json.dumps(alert)


@pytest.mark.parametrize("method,status", [("approval", "waitingForApproval"), ("clarify", "waitingForClarification")])
def test_server_requests_project_attention_and_cancel_from_canonical_registry(push_service, peer, method, status):
    from tui_gateway import server_requests
    from tui_gateway.mobile_push import Scope
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    opened = open_bot()
    assert "result" in rpc("mobile.push.register", **registration(opened))
    sid = opened["session_id"]
    server._emit("message.start", sid)
    push_scope = Scope("native", opened["profile"], opened["canonical_root_id"])
    # Use the real registry/write sink, including detached delivery and idempotent replay.
    server._sessions[sid]["transport"] = server._detached_ws_transport
    params = ({"request_id": "fixture-approval", "command": "fixture", "choices": ["once", "deny"]} if method == "approval"
              else {"question": "Fixture choice?", "choices": ["Blue", "Green"]})
    try:
        server_requests.send_async(method, sid, params, lambda result: None)
        frame = server_requests.open_requests(sid)[0]
        assert push_service.current_run(push_scope)["status"] == status
        server.write_json({"jsonrpc": "2.0", **frame})
        push_service.drain_once()
        assert len(push_service.sender.jobs) == 1
        assert server_requests.cancel(sid, "timeout") == 1
        assert push_service.current_run(push_scope)["status"] == "thinking"
        assert server_requests.open_requests(sid) == []
    finally:
        server_requests.cancel(sid)
