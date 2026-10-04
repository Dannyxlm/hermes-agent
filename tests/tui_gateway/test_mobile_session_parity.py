"""Ordinary-session delivery and publication-cut contracts through real RPCs."""
import threading
import uuid

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc, open_bot
from tests.tui_gateway.test_methods_mobile_push import push_service
from tui_gateway import server, event_replay


@pytest.fixture
def ordinary(mobile_home, peer):
    peer.auth_identity = {"user_id": "fixture-owner", "provider": "fixture-auth"}
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("ordinary-root", "desktop")
        db._conn.execute("UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id='ordinary-root'")
        db.create_session("ordinary-tip", "desktop", parent_session_id="ordinary-root")
    return {"profile": "ops", "stored_session_id": "ordinary-root",
            "installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4()),
            "device_token": "ab" * 32, "environment": "production"}


def live(mobile_home, peer):
    sid = "ordinary-runtime"
    server._sessions[sid] = {"session_key": "ordinary-tip", "profile_home": str(mobile_home / "profiles" / "ops"),
                             "history_lock": threading.RLock(), "transport": peer}
    return sid


def test_ordinary_registration_resolves_tip_and_rejects_cross_profile(push_service, ordinary):
    result = rpc("mobile.session_push.register", **ordinary)["result"]
    assert result["destination"] == {"surface": "native_session", "profile": "ops", "session_id": "ordinary-root"}
    assert result["resolved_session_id"] == "ordinary-tip"
    for override in ({"profile": "default"}, {"profile": "absent"}, {"stored_session_id": "missing"},
                     {"stored_session_id": "ops-root"}):
        assert rpc("mobile.session_push.register", **{**ordinary, **override})["error"]["code"] == 4400
    refresh = {k: ordinary[k] for k in ("installation_id", "connection_id", "device_token", "environment")}
    assert rpc("mobile.session_push.refresh", **refresh)["result"]["updated"] == 1
    identity = {k: ordinary[k] for k in ("installation_id", "connection_id")}
    assert rpc("mobile.session_push.unregister", **identity)["result"]["removed"] == 1


@pytest.mark.parametrize("event,status", [("message.complete", "complete"), ("message.complete", "error"),
                                         ("approval", "waitingForApproval"), ("clarify", "waitingForClarification")])
def test_ordinary_detached_projection(push_service, ordinary, mobile_home, peer, event, status):
    assert "result" in rpc("mobile.session_push.register", **ordinary)
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    session = server._sessions[sid]
    run_id = session["_mobile_push_run_id"]
    activity = {**{k: v for k, v in ordinary.items() if k != "device_token"}, "activity_token": "cd" * 32, "activity_id": "fixture-activity", "run_id": run_id}
    assert "result" in rpc("mobile.session_activity.register", **activity)
    session["transport"] = server._detached_ws_transport
    if event in {"approval", "clarify"}:
        server.write_json({"method": event, "id": "fixture-request", "params": {"session_id": sid}})
    else:
        server._emit(event, sid, {"status": status, "text": "fixture final"})
    push_service.drain_once()
    alerts = [j for j in push_service.sender.jobs if j["kind"] == "alert"]
    assert len(alerts) == 1
    assert alerts[0]["payload"]["hermex.destination"]["surface"] == "native_session"
    assert alerts[0]["payload"]["hermex.destination"]["session_id"] == "ordinary-root"
    assert alerts[0]["payload"]["hermex.status"] == ("failed" if status == "error" else status)
    assert any(j["kind"] == "activity" for j in push_service.sender.jobs)


def test_widget_native_scope_and_capability(push_service, ordinary, mobile_home, peer):
    result = rpc("mobile.widget.register", **{k: v for k, v in ordinary.items() if k != "device_token"}, read_token="12" * 32, widget_token="")["result"]
    assert result["snapshot_path"] == "/api/mobile/widgets/snapshot"
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    assert push_service.store.widget_snapshot("12" * 32)["items"][0]["session_id"] == "ordinary-root"
    assert rpc("mobile.widget.unregister", **{k: ordinary[k] for k in ("installation_id", "connection_id")})["result"]["removed"] == 1
    with pytest.raises(PermissionError):
        push_service.store.widget_snapshot("12" * 32)


def test_stream_snapshot_excludes_unpublished_text_and_replays_once(mobile_home, peer, ordinary):
    event_replay.reset_replay_state()
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    server._emit("message.delta", sid, {"text": "before"})
    # A producer can mutate inflight BEFORE publishing a delta. The cut must not include it.
    server._sessions[sid]["inflight_turn"] = {"assistant": "beforeafter", "user": "fixture"}
    cut = rpc("session.stream.snapshot", session_id=sid)["result"]
    assert cut["session_id"] == sid
    assert cut["epoch"] == event_replay.replay_epoch()
    assert cut["stream"]["assistant"] == "before"
    server._emit("message.delta", sid, {"text": "after"})
    replay = event_replay.events_since(sid, cut["baseline_seq"])
    assert [e["payload"]["text"] for e in replay] == ["after"]
    assert cut["stream"]["assistant"] + "".join(e["payload"]["text"] for e in replay) == "beforeafter"
    assert event_replay.events_since(sid, replay[-1]["seq"]) == []


def test_stream_snapshot_interim_and_terminal_are_authoritative(mobile_home, peer, ordinary):
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    server._emit("message.delta", sid, {"text": "commentary"})
    server._emit("message.interim", sid, {"text": "commentary", "already_streamed": True})
    server._emit("message.delta", sid, {"text": "partial"})
    server._emit("message.complete", sid, {"text": "final", "status": "complete"})
    cut = rpc("session.stream.snapshot", session_id=sid)["result"]
    assert cut["stream"]["segments"] == ["commentary"]
    assert cut["stream"]["assistant"] == "final"
    assert cut["stream"]["status"] == "complete"
    assert event_replay.events_since(sid, cut["baseline_seq"]) == []


def test_widget_exact_path_never_grants_general_access(push_service, ordinary):
    import asyncio
    from starlette.requests import Request
    from tui_gateway.mobile_widget_http import widget_snapshot_response
    assert "result" in rpc("mobile.widget.register", **{k: v for k, v in ordinary.items() if k != "device_token"}, read_token="12" * 32, widget_token="")
    for path, method in [("/api/sessions", "GET"), ("/api/ws", "GET"),
                         ("/api/mobile/widgets/snapshot/extra", "GET"), ("/api/mobile/widgets/snapshot", "POST")]:
        request = Request({"type": "http", "path": path, "method": method,
                           "headers": [(b"authorization", b"Bearer " + b"12" * 32)]})
        assert asyncio.run(widget_snapshot_response(request, service=push_service)) is None
    request = Request({"type": "http", "path": "/api/mobile/widgets/snapshot", "method": "GET",
                       "headers": [(b"authorization", b"Bearer " + b"12" * 32)]})
    response = asyncio.run(widget_snapshot_response(request, service=push_service))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_widget_http_seam_is_read_only(push_service, ordinary, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_cli import web_server
    assert "result" in rpc("mobile.widget.register", **{k: v for k, v in ordinary.items() if k != "device_token"},
                           read_token="12" * 32)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    client = TestClient(web_server.app)
    headers = {"Authorization": "Bearer " + "12" * 32}
    response = client.get("/api/mobile/widgets/snapshot", headers=headers)
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.headers["cache-control"] == "no-store"
    for path in ("/api/sessions", "/api/config", "/api/mobile/widgets/snapshot/extra"):
        assert client.get(path, headers=headers).status_code == 401
    assert client.post("/api/mobile/widgets/snapshot", headers=headers).status_code == 401
    assert client.get("/api/mobile/widgets/snapshot", headers={"Authorization": "Bearer " + "34" * 32}).status_code == 401


def test_presence_suppresses_only_registered_foreground_device(push_service, ordinary, mobile_home, peer):
    assert "result" in rpc("mobile.session_push.register", **ordinary)
    presence = {k: ordinary[k] for k in ("profile", "stored_session_id", "installation_id", "connection_id")}
    assert rpc("mobile.session_push.presence", **presence, foreground=True)["result"]["foreground"] is True
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    server._emit("message.complete", sid, {"status": "complete", "text": "fixture"})
    push_service.drain_once()
    assert push_service.sender.jobs == []
    assert rpc("mobile.session_push.presence", **presence, foreground=False)["result"]["foreground"] is False
    server._emit("message.start", sid)
    server._emit("message.complete", sid, {"status": "complete"})
    push_service.drain_once()
    assert len(push_service.sender.jobs) == 1


def test_presence_lease_expiry_and_pending_retry_suppression(push_service, ordinary, mobile_home, peer):
    now = [1000.0]
    push_service.store.clock = lambda: now[0]
    rpc("mobile.session_push.register", **ordinary)
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    server._emit("message.complete", sid, {"status": "complete"})
    presence = {k: ordinary[k] for k in ("profile", "stored_session_id", "installation_id", "connection_id")}
    assert "result" in rpc("mobile.session_push.presence", **presence, foreground=True)
    push_service.drain_once()
    assert push_service.sender.jobs == []
    now[0] += 61
    server._emit("message.start", sid)
    server._emit("message.complete", sid, {"status": "complete"})
    push_service.drain_once()
    assert len(push_service.sender.jobs) == 1


def test_snapshot_blocks_between_stamp_and_projection(mobile_home, peer, ordinary, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tui_gateway import session_stream_cut
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    stamped, release, snapshot_entered = threading.Event(), threading.Event(), threading.Event()
    original = session_stream_cut.project_event
    def pause(session, frame):
        stamped.set()
        assert release.wait(5)
        original(session, frame)
    monkeypatch.setattr(session_stream_cut, "project_event", pause)
    def snapshot():
        snapshot_entered.set()
        return session_stream_cut.stream_cut(sid, server._sessions[sid])
    with ThreadPoolExecutor(max_workers=2) as executor:
        producer = executor.submit(server._emit, "message.delta", sid, {"text": "atomic"})
        assert stamped.wait(5)
        reader = executor.submit(snapshot)
        assert snapshot_entered.wait(5)
        assert not reader.done()
        release.set()
        producer.result(5)
        cut = reader.result(5)
    assert cut["stream"]["assistant"] == "atomic"
    assert event_replay.events_since(sid, cut["baseline_seq"]) == []


def test_resume_exposes_optional_published_cut(mobile_home, peer, ordinary):
    resumed = rpc("session.resume", profile="ops", session_id="ordinary-root",
                  defer_history=True, omit_messages=True)["result"]
    sid = resumed["session_id"]
    assert resumed["stream_snapshot"]["session_id"] == sid
    assert resumed["stream_snapshot"]["stream"] is None
    server._emit("message.start", sid)
    server._emit("message.delta", sid, {"text": "published"})
    server._sessions[sid]["inflight_turn"] = {"assistant": "publishedNOT-YET-PUBLISHED"}
    resumed = rpc("session.resume", profile="ops", session_id="ordinary-root",
                  defer_history=True, omit_messages=True)["result"]
    cut = resumed["stream_snapshot"]
    assert cut["session_id"] == sid
    assert cut["stream"]["assistant"] == "published"
    assert cut["epoch"] == event_replay.replay_epoch()
    server._emit("message.delta", sid, {"text": "-later"})
    replay = rpc("session.events.since", session_id=sid, last_seen=cut["baseline_seq"])["result"]
    assert [e["payload"]["text"] for e in replay["events"]] == ["-later"]


def test_snapshot_rejects_other_connection_and_profile(mobile_home, peer, ordinary):
    from tui_gateway.transport import bind_transport, reset_transport
    from tests.tui_gateway.test_methods_mobile import MobilePeer
    sid = live(mobile_home, peer)
    assert "error" in rpc("session.stream.snapshot", session_id=sid, profile="default")
    token = bind_transport(MobilePeer())
    try:
        assert "error" in rpc("session.stream.snapshot", session_id=sid)
    finally:
        reset_transport(token)
