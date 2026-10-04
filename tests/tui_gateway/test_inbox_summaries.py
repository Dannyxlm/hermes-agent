"""Inbox evidence uses real requests and profile DBs, never attaches a runtime."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server, server_requests


@pytest.fixture
def inbox(mobile_home, monkeypatch):
    from hermes_cli.web_routers import sessions
    monkeypatch.setattr(server, "_resolve_model", lambda: "fixture")
    monkeypatch.setattr(sessions, "_maybe_auto_archive_for_profile", lambda profile: None)
    for profile, home in [("default", mobile_home), ("ops", mobile_home / "profiles" / "ops")]:
        with SessionDB(db_path=home / "state.db") as db:
            db.create_session("same", "desktop")
            db.set_session_title("same", "Ordinary")
        server._sessions[profile] = {"session_key": "same", "profile_home": str(home) if profile != "default" else None,
                                    "history": [], "created_at": 100, "last_active": 200}
    app = FastAPI()
    app.include_router(sessions.list_router)
    return TestClient(app)


def request(sid, method):
    req = server_requests.ServerRequest(sid, method, {"private": "secret sentinel"})
    with server_requests._lock:
        server_requests._open[req.id] = req
    return req


def row(client, profile):
    response = client.get("/api/sessions", params={"profile": profile})
    assert response.status_code == 200, response.text
    return next(r for r in response.json()["sessions"] if r["id"] == "same")


@pytest.mark.parametrize("method,kind", [("approval", "approval"), ("clarify", "clarify"), ("sudo", "input")])
def test_pending_summary_is_profile_scoped_and_clears(inbox, peer, method, kind):
    req = request("ops", method)
    attention = row(inbox, "ops")["attention"]
    assert attention["kind"] == kind
    assert attention["count"] == 1
    assert isinstance(attention["revision"], str) and attention["revision"]
    assert row(inbox, "ops")["attention"] == attention
    assert "attention" not in row(inbox, "default")
    live = rpc("session.active_list", profile="ops")["result"]["sessions"][0]
    assert (live["pending_kind"], live["pending_count"]) == (kind, 1)
    assert live["pending_revision"] == attention["revision"]
    assert live["status"] == "waiting"
    assert "secret sentinel" not in json.dumps(live) + json.dumps(attention)
    assert server_requests.resolve_response({"id": req.id, "result": {"value": "yes"}})
    assert "attention" not in row(inbox, "ops")
    live = rpc("session.active_list", profile="ops")["result"]["sessions"][0]
    assert "pending_kind" not in live and "pending_count" not in live
    assert "attention" not in row(inbox, "default")


def test_launch_profile_attention_with_implicit_home(inbox):
    request("default", "approval")
    assert row(inbox, "default")["attention"]["count"] == 1
    assert "attention" not in row(inbox, "ops")


def test_generic_waiting_is_not_typed_attention(inbox, peer):
    request("ops", "preview.read")
    assert "attention" not in row(inbox, "ops")
    live = rpc("session.active_list", profile="ops")["result"]["sessions"][0]
    assert live["status"] == "waiting"
    assert "pending_kind" not in live


def test_final_reply_is_page_batched_and_profile_isolated(inbox, mobile_home, monkeypatch):
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        final_id = db.append_message("same", "assistant", "final reply", timestamp=123, finish_reason="stop")
        db.append_message("same", "assistant", "tool preamble", timestamp=124, finish_reason="tool_calls")
        db.append_message("same", "assistant", "", timestamp=125, finish_reason="stop")
        db.append_message("same", "assistant", "   \n", timestamp=126, finish_reason="stop")
        db.append_message("same", "user", "new prompt", timestamp=127)
        for i in range(5):
            db.create_session(f"extra-{i}", "desktop")
            db.append_message(f"extra-{i}", "assistant", "reply", timestamp=120 + i)
    queries = []
    read_all = SessionDB._read_all
    def counted(self, sql, *args, **kwargs):
        queries.append(sql)
        return read_all(self, sql, *args, **kwargs)
    monkeypatch.setattr(SessionDB, "_read_all", counted)
    response = inbox.get("/api/sessions", params={"profile": "ops"}).json()
    summaries = [r for r in response["sessions"] if "last_assistant_reply" in r]
    assert len(summaries) == 6
    assert next(r for r in summaries if r["id"] == "same")["last_assistant_reply"] == {"row_id": final_id, "at": 123}
    assert len([sql for sql in queries if "MAX(id)" in sql and "finish_reason" in sql]) == 1
    assert "last_assistant_reply" not in row(inbox, "default")


def test_archived_count_respects_list_scope(inbox, mobile_home):
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        db.create_session("archived", "desktop")
        db.set_session_archived("archived", True)
    response = inbox.get("/api/sessions", params={"profile": "ops"}).json()
    assert response["archived_count"] == 1
    assert response["total"] == 2  # ordinary + Bot Chat, old count semantics
    assert inbox.get("/api/sessions", params={"profile": "default"}).json()["archived_count"] == 0


def test_latest_run_reads_unenrolled_ordinary_scope_without_mutation(inbox, mobile_home, peer):
    from tui_gateway.mobile_push import PushService
    from tui_gateway.mobile_push_payloads import Scope
    from tests.tui_gateway.test_mobile_push import Sender
    service = PushService(mobile_home / "mobile-push" / "outbox.sqlite3", Sender(), clock=lambda: 321)
    try:
        run = service.start_run(Scope("native_session", "ops", "same"), run_id="real-run")
        service.record(Scope("native_session", "ops", "same"), run["run_id"], "finish", "failed")
        before = dict(server._sessions["ops"])
        live = rpc("session.active_list", profile="ops")["result"]["sessions"][0]
        assert live["latest_run"] == {"run_id": "real-run", "status": "failed", "at": 321}
        assert server._sessions["ops"] == before
        assert live["session_key"] == "same" and live["status"] == "idle"
        assert "latest_run" not in rpc("session.active_list", profile="default")["result"]["sessions"][0]
        assert service.store._db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    finally:
        service.close()


def test_capability_advertises_inbox_v1(mobile_home, peer):
    capabilities = rpc("mobile.capabilities")["result"]
    assert capabilities["feature_versions"]["inbox_summaries"] == 1
    assert capabilities["feature_versions"]["rich_stream_snapshot"] == 1


def test_scope_completeness_is_process_and_page_only(inbox, peer):
    scope = inbox.get("/api/sessions", params={"profile": "ops"}).json()["inbox_summary_scope"]
    assert scope["profile"] == "ops"
    assert scope["pending_scope"] == "process" and scope["pending_complete"] is True
    assert scope["replies_complete"] is True
    live_scope = rpc("session.active_list", profile="ops")["result"]["inbox_summary_scope"]
    assert live_scope["pending_epoch"] == scope["pending_epoch"]
    assert live_scope["pending_complete"] is True
    assert "replies_complete" not in live_scope


def test_compression_reply_and_run_do_not_follow_branches(inbox, mobile_home, peer):
    from tui_gateway.mobile_push import PushService
    from tui_gateway.mobile_push_payloads import Scope
    from tests.tui_gateway.test_mobile_push import Sender
    home = mobile_home / "profiles" / "ops"
    with SessionDB(db_path=home / "state.db") as db:
        final_id = db.append_message("same", "assistant", "root final", timestamp=100, finish_reason="stop")
        db._conn.execute("UPDATE sessions SET end_reason='compression', ended_at=101 WHERE id='same'")
        db.create_session("tip", "desktop", parent_session_id="same")
        db.append_message("tip", "assistant", "tool", timestamp=102, finish_reason="tool_calls")
        db.create_session("branch", "desktop", parent_session_id="same", model_config={"_branched_from": "same"})
        db.append_message("branch", "assistant", "branch reply", timestamp=103, finish_reason="stop")
    server._sessions["ops"]["session_key"] = "tip"
    server._sessions["branch"] = {"session_key": "branch", "profile_home": str(home)}
    request("ops", "approval")
    response = inbox.get("/api/sessions", params={"profile": "ops"}).json()
    tip = next(r for r in response["sessions"] if r["id"] == "tip")
    assert tip["last_assistant_reply"] == {"row_id": final_id, "at": 100}
    assert tip["attention"]["kind"] == "approval"
    service = PushService(mobile_home / "mobile-push" / "outbox.sqlite3", Sender(), clock=lambda: 321)
    try:
        service.start_run(Scope("native_session", "ops", "same"), run_id="root-run")
        rows = rpc("session.active_list", profile="ops")["result"]["sessions"]
        assert next(r for r in rows if r["id"] == "ops")["latest_run"]["run_id"] == "root-run"
        assert "latest_run" not in next(r for r in rows if r["id"] == "branch")
    finally:
        service.close()
