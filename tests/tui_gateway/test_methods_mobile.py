"""Strict mobile entry points exercise the real dispatcher and isolated profile databases."""

import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport

_REAL_HYDRATION = server._schedule_resume_hydration


class MobilePeer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True

    def close(self):
        pass


@pytest.fixture
def mobile_home(tmp_path, monkeypatch):
    from tui_gateway import server_requests
    server_requests.reset_for_tests()
    home = tmp_path / ".hermes"
    (home / "profiles" / "ops").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_sessions", {})
    for path in (home, home / "profiles" / "ops"):
        with SessionDB(db_path=path / "state.db") as db:
            db.create_session("root" if path == home else "ops-root", "desktop")
            db._conn.execute("UPDATE sessions SET title = 'Bot Chat'")
    launch_db = SessionDB(db_path=home / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: launch_db)
    def hydration(sid, key, db, *, close_db=False, tip_only=False, model_history_only=False):
        if close_db:
            db.close()
        server._sessions[sid]["resume_hydrating"] = False
        server._sessions[sid]["resume_history_ready"].set()
    monkeypatch.setattr(server, "_schedule_resume_hydration", hydration)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    yield home
    launch_db.close()


@pytest.fixture
def peer():
    client = MobilePeer()
    token = bind_transport(client)
    yield client
    reset_transport(token)


def open_bot(profile="ops", root="ops-root", **params):
    response = rpc("mobile.open", profile=profile, canonical_root_id=root, **params)
    assert "result" in response, response
    return response["result"]


def scope(snapshot):
    return {key: snapshot[key] for key in ("profile", "canonical_root_id", "session_id")}


def rpc(method, **params):
    return server.handle_request({"id": "mobile-test", "method": method, "params": params})


@pytest.mark.parametrize("target", ["root", "missing", "Bot Chat"])
def test_strict_resume_never_adopts_or_resolves_titles(mobile_home, target):
    response = rpc("session.resume", session_id=target, profile="ops",
                   strict_canonical_root_id=target, defer_history=True, omit_messages=True)
    assert "error" in response
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db", read_only=True) as db:
        assert [row[0] for row in db._conn.execute("SELECT id FROM sessions")] == ["ops-root"]


def test_missing_profile_cannot_create_database(mobile_home):
    response = rpc("mobile.open", profile="absent", canonical_root_id="root")
    assert "error" in response
    assert not (mobile_home / "profiles" / "absent").exists()


def test_foreign_root_missing_canonical_never_adopts(mobile_home, peer):
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        db._conn.execute("DELETE FROM sessions")
    assert "error" in rpc("mobile.open", profile="ops", canonical_root_id="root")
    assert "error" in rpc("session.resume", profile="ops", session_id="root",
                          strict_canonical_root_id="root")
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db", read_only=True) as db:
        assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("profile", ["../ops", "ops/..", "OPS", "", None, 4])
def test_profile_identity_is_strict(mobile_home, peer, profile):
    assert "error" in rpc("mobile.open", profile=profile, canonical_root_id="ops-root")


def test_capabilities_and_roster_are_bounded_and_path_free(mobile_home, peer):
    assert rpc("mobile.capabilities")["result"]["protocol_version"] == 1
    page = rpc("mobile.bots", limit=1)["result"]
    assert len(page["bots"]) == 1
    assert page["next_offset"] == 1
    assert "path" not in page["bots"][0]
    assert page["bots"][0]["working"] is None
    assert "error" in rpc("mobile.bots", limit=101)
    assert "error" in rpc("mobile.bots", limit=True)
    assert rpc("mobile.bots", offset=1)["result"]["bots"][0]["canonical_session"]["id"] == "ops-root"


def test_real_dispatch_opens_same_runtime_twice_and_bounds_history(mobile_home, peer, monkeypatch):
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        for n in range(7):
            db.append_message("ops-root", "user" if n % 2 == 0 else "assistant", f"message {n}")
    def forbid_full_history(*args, **kwargs):
        raise AssertionError("mobile open must not load the full display transcript")
    monkeypatch.setattr(SessionDB, "get_messages_as_conversation", forbid_full_history)
    opened = open_bot(limit=2)
    assert [m["text"] for m in opened["history"]["messages"]] == ["message 5", "message 6"]
    assert opened["history"]["has_more"]
    assert open_bot(limit=2)["session_id"] == opened["session_id"]
    oldest = opened["history"]["before_row_id"]
    page = rpc("mobile.snapshot", **scope(opened), limit=2, before_row_id=oldest)["result"]["history"]
    assert [m["text"] for m in page["messages"]] == ["message 3", "message 4"]
    assert page["before_row_id"] < oldest


def test_compression_follows_only_canonical_chain(mobile_home, peer):
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        db.append_message("ops-root", "user", "before compression")
        db._conn.execute("UPDATE sessions SET end_reason = 'compression', ended_at = 1 WHERE id = 'ops-root'")
        db.create_session("tip", "desktop", parent_session_id="ops-root")
        db.append_message("tip", "assistant", "after compression")
        db.create_session("branch", "desktop", parent_session_id="ops-root",
                          model_config={"_branched_from": "ops-root"})
        db.append_message("branch", "user", "branch must remain separate")
    opened = open_bot()
    assert opened["canonical_root_id"] == "ops-root"
    assert opened["stored_session_id"] == "tip"
    assert [m["text"] for m in opened["history"]["messages"]] == ["before compression", "after compression"]


def test_compaction_paging_dedupes_full_text_and_keeps_tool_result(mobile_home, peer):
    path = mobile_home / "profiles" / "ops" / "state.db"
    long_text = "full text " * 1000
    with SessionDB(db_path=path) as db:
        db.append_message("ops-root", "user", long_text, timestamp=100)
        db._conn.execute("UPDATE messages SET active=0, compacted=1")
        db.append_message("ops-root", "user", long_text, timestamp=100)
        db.append_message("ops-root", "tool", "complete tool result", tool_name="terminal", tool_call_id="tool-1")
    opened = open_bot(limit=1)
    assert opened["history"]["messages"][0]["text"] == "complete tool result"
    page = rpc("mobile.snapshot", **scope(opened), limit=1,
               before_row_id=opened["history"]["before_row_id"])["result"]["history"]
    assert page["messages"][0]["text"] == long_text.strip()
    assert not page["has_more"]


def test_absent_archived_and_duplicate_canonical_are_not_repaired(mobile_home, peer):
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        db._conn.execute("UPDATE sessions SET archived=1, end_reason='ws_orphan_reap'")
    assert "error" in rpc("mobile.open", profile="ops", canonical_root_id="ops-root")
    assert rpc("mobile.bots")["result"]["bots"][1]["canonical_session"] is None
    with SessionDB(db_path=path) as db:
        assert db.get_session("ops-root")["archived"]
        db._conn.execute("UPDATE sessions SET archived=0")
        db.create_session("duplicate", "desktop")
        # Simulate an older/corrupt registry whose unique-title index is missing.
        db._conn.execute("DROP INDEX idx_sessions_title_unique")
        db._conn.execute("UPDATE sessions SET title='Bot Chat' WHERE id='duplicate'")
    assert "error" in rpc("mobile.open", profile="ops", canonical_root_id="ops-root")


def test_snapshot_requires_attached_peer_and_exact_profile(mobile_home, peer):
    opened = open_bot()
    assert "error" in rpc("mobile.snapshot", **{**scope(opened), "profile": "default"})
    token = bind_transport(MobilePeer())
    try:
        assert "error" in rpc("mobile.snapshot", **scope(opened))
    finally:
        reset_transport(token)


@pytest.mark.parametrize("text", ["/new", "  /reset", "\n/slash", "\x1b[31m/new"])
def test_slash_commands_rejected_at_server(mobile_home, peer, text):
    opened = open_bot()
    assert "error" in rpc("mobile.submit", **scope(opened), text=text)


def test_mobile_submit_always_queues_at_real_busy_dispatch(mobile_home, peer, monkeypatch):
    opened = open_bot()
    session = server._sessions[opened["session_id"]]
    session["running"] = True
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    # Queue policy is server-owned; strict wire validation rejects a client override.
    assert "error" in rpc("mobile.submit", **scope(opened), text="next request", queued=False)
    response = rpc("mobile.submit", **scope(opened), text="next request")
    assert response["result"]["status"] == "queued"
    assert response["result"]["accepted"] is True
    assert session["queued_prompt"]["text"] == "next request"


def test_exact_approval_rejects_other_profile_stale_and_persistent_choice(mobile_home, peer, monkeypatch):
    import tools.approval as approvals
    opened = open_bot()
    event = threading.Event()
    entry = SimpleNamespace(data={"request_id": "approval-1", "command": "echo safe"}, event=event, result=None)
    monkeypatch.setattr(approvals, "_gateway_queues", {"ops-root": [entry]})
    assert rpc("mobile.approval.respond", **scope(opened), request_id="other", choice="once")["result"]["resolved"] == 0
    assert "error" in rpc("mobile.approval.respond", **scope(opened), request_id="approval-1", choice="always")
    assert "error" in rpc("mobile.approval.respond", **{**scope(opened), "profile": "default"},
                          request_id="approval-1", choice="once")
    assert not event.is_set()
    assert rpc("mobile.approval.respond", **scope(opened), request_id="approval-1", choice="once")["result"]["resolved"] == 1
    assert entry.result == "once"
    assert event.is_set()
    assert rpc("mobile.approval.respond", **scope(opened), request_id="approval-1", choice="once")["result"]["resolved"] == 0


def test_pending_redaction_and_offered_choices(mobile_home, peer, monkeypatch):
    import tools.approval as approvals
    opened = open_bot()
    entry = SimpleNamespace(data={"request_id": "approval-1", "command": "echo PRIVATE_FIXTURE", "choices": ["deny"]},
                            event=threading.Event(), result=None)
    monkeypatch.setattr(approvals, "_gateway_queues", {"ops-root": [entry]})
    monkeypatch.setattr("gateway.run._redact_approval_command", lambda command: "redacted fixture")
    safe = rpc("mobile.snapshot", **scope(opened))["result"]["pending_approvals"][0]
    assert safe["command"] == "redacted fixture"
    assert safe["choices"] == ["deny"]
    assert "error" in rpc("mobile.approval.respond", **scope(opened), request_id="approval-1", choice="once")
    assert not entry.event.is_set()


def test_exact_clarify_owner_type_and_duplicate(mobile_home, peer, monkeypatch):
    opened = open_bot()
    sid = opened["session_id"]
    from tui_gateway import server_requests as requests
    own = requests.ServerRequest(sid, "clarify", {"question": "Color?"})
    foreign = requests.ServerRequest("another-runtime", "clarify", {"question": "Color?"})
    secret = requests.ServerRequest(sid, "secret", {"env_var": "TEST", "prompt": "Secret"})
    for request in (own, foreign, secret):
        requests._register(request)
    for request in (foreign, secret):
        assert "error" in rpc("mobile.clarify.respond", **scope(opened), request_id=request.id, answer="answer")
    assert rpc("mobile.clarify.respond", **scope(opened), request_id=own.id, answer="answer")["result"]["status"] == "ok"
    assert own.result == {"answer": "answer"}
    assert not foreign.event.is_set() and not secret.event.is_set()
    assert rpc("mobile.clarify.respond", **scope(opened), request_id=own.id, answer="changed")["result"]["status"] == "expired"
    assert own.result == {"answer": "answer"}


def test_clarify_batch_snapshot_locks_and_cancellation(mobile_home, peer):
    from tui_gateway import server_requests as requests
    opened = open_bot()
    sid = opened["session_id"]
    request = requests.ServerRequest(sid, "clarify", {"questions": [
        {"qid": "a", "question": "A?"}, {"qid": "b", "question": "B?"}]}, qids=["a", "b"])
    requests._register(request)
    snapshot = rpc("mobile.snapshot", **scope(opened))["result"]
    assert snapshot["pending_clarify"]["request_id"] == request.id
    assert snapshot["open_requests"][0]["id"] == request.id
    assert "error" in rpc("mobile.clarify.respond", **scope(opened), request_id=request.id, answer="no batch id")
    assert "error" in rpc("mobile.clarify.respond", **scope(opened), request_id=request.id, question_id="bad", answer="x")
    result = rpc("mobile.clarify.respond", **scope(opened), request_id=request.id, question_id="a", answer="one")
    assert result["result"]["remaining"] == ["b"]
    assert rpc("mobile.snapshot", **scope(opened))["result"]["pending_clarify"]["answers"] == {"a": "one"}
    assert rpc("mobile.clarify.respond", **scope(opened), request_id=request.id, question_id="b", answer="two")["result"]["remaining"] == []
    assert request.result == {"answers": {"a": "one", "b": "two"}}
    assert rpc("mobile.clarify.respond", **scope(opened), request_id=request.id, question_id="a", answer="changed")["result"]["status"] == "expired"
    second = requests.ServerRequest(sid, "clarify", {"question": "Cancel?"})
    requests._register(second)
    requests.cancel(sid)
    assert rpc("mobile.snapshot", **scope(opened))["result"]["pending_clarify"] is None
    assert rpc("mobile.clarify.respond", **scope(opened), request_id=second.id, answer="late")["result"]["status"] == "expired"


@pytest.mark.parametrize("choice", ["once", "deny"])
def test_mobile_canonical_approval_fenced_and_once(mobile_home, peer, choice):
    from tui_gateway import server_requests as requests
    opened = open_bot()
    outcomes = []
    request = requests.ServerRequest(opened["session_id"], "approval",
        {"request_id": "queue-entry", "choices": ["once", "deny"]}, on_result=outcomes.append)
    requests._register(request)
    assert "error" in rpc("mobile.approval.respond", **scope(opened), request_id=request.id, choice="always")
    assert rpc("mobile.approval.respond", **scope(opened), request_id=request.id, choice=choice)["result"]["resolved"] == 1
    assert rpc("mobile.approval.respond", **scope(opened), request_id=request.id, choice=choice)["result"]["resolved"] == 0
    assert outcomes == [{"choice": choice}]


def test_legacy_title_resume_unchanged(mobile_home, peer):
    response = rpc("session.resume", session_id="Bot Chat", profile="ops", defer_history=True, omit_messages=True)
    assert response["result"]["resumed"] == "ops-root"


def test_real_cold_hydration_never_loads_ancestor_display(mobile_home, peer, monkeypatch):
    monkeypatch.setattr(server, "_schedule_resume_hydration", _REAL_HYDRATION)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: None)
    def forbid_full_history(*args, **kwargs):
        raise AssertionError("strict hydration loaded the full lineage")
    monkeypatch.setattr(server, "_load_resume_transcript", forbid_full_history)
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        db.append_message("ops-root", "user", "model context")
    opened = open_bot()
    session = server._sessions[opened["session_id"]]
    assert session["resume_history_ready"].wait(timeout=5)
    assert not session.get("resume_history_error")
    assert session["history"][0]["content"] == "model context"
    assert not session.get("display_history_prefix")


def test_legacy_adoption_compatibility_stays_outside_strict_lane(mobile_home, peer):
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        db._conn.execute("DELETE FROM sessions")
    response = rpc("session.resume", session_id="root", profile="ops", defer_history=True, omit_messages=True)
    assert response["result"]["resumed"] == "root"
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db", read_only=True) as db:
        assert db.get_session("root") is not None


def test_tool_call_only_assistant_is_not_lost(mobile_home, peer):
    tool_calls = [{"id": "call-1", "type": "function", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}}]
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        db.append_message("ops-root", "assistant", "", tool_calls=tool_calls)
    messages = open_bot()["history"]["messages"]
    assert messages[0]["tool_calls"] == tool_calls
    assert messages[0]["message_id"] == f"{messages[0]['row_id']}:0"


def test_mobile_stop_targets_runtime_and_leaves_global_voice(mobile_home, peer, monkeypatch):
    opened = open_bot()
    sid = opened["session_id"]
    monkeypatch.setattr(server, "_sess", lambda params, rid: (server._sessions[sid], None))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *args: False)
    stopped = []
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda target, record, **kw: stopped.append(target))
    def no_global_voice():
        raise AssertionError("mobile text stop touched process-global voice")
    monkeypatch.setattr(server, "_tts_stream_stop", no_global_voice)
    assert rpc("mobile.stop", **scope(opened))["result"]["status"] == "interrupted"
    assert stopped == [sid]
    assert "error" in rpc("mobile.stop", **{**scope(opened), "profile": "default"})
    assert stopped == [sid]


@pytest.mark.parametrize("method", ["mobile.bots", "mobile.open", "mobile.snapshot"])
def test_blocked_mobile_read_keeps_control_frames_and_transport_context(mobile_home, peer, monkeypatch, method):
    from tui_gateway.transport import current_transport

    opened = open_bot()
    params = {"limit": 1} if method == "mobile.bots" else scope(opened)
    if method == "mobile.open":
        params.pop("session_id")
    entered, release, control_seen, response_seen = (threading.Event() for _ in range(4))
    requests = []
    read_db = server._mobile_read_db
    def blocked_read(home):
        requests.append(current_transport())
        entered.set()
        assert release.wait(10), "test did not release the database read"
        return read_db(home)
    monkeypatch.setattr(server, "_mobile_read_db", blocked_read)
    write = peer.write
    def receive(frame):
        result = write(frame)
        if frame.get("id") == "blocked-read":
            response_seen.set()
        return result
    monkeypatch.setattr(peer, "write", receive)
    def read_frames():
        server.dispatch({"id": "blocked-read", "method": method, "params": params}, peer)
        control = server.dispatch({"id": "following-control", "method": "ping", "params": {}}, peer)
        assert control["result"]["pong"] is True
        control_seen.set()
    reader = threading.Thread(target=read_frames)
    reader.start()
    try:
        assert entered.wait(5)
        assert control_seen.wait(5), "blocked read prevented the following control frame"
    finally:
        release.set()
        reader.join(timeout=5)
    assert not reader.is_alive()
    assert response_seen.wait(5)
    assert requests and all(transport is peer for transport in requests)
    response = next(frame for frame in peer.frames if frame.get("id") == "blocked-read")
    assert "result" in response, response


def test_snapshot_returns_full_current_plan_and_empty_clear(mobile_home, peer):
    opened = open_bot()
    session = server._sessions[opened["session_id"]]
    steps = [{"id": "parent", "content": "Build", "status": "in_progress"},
             {"id": "child", "parent": "parent", "content": "Check", "status": "completed"}]
    session["todo_state"] = {"todos": steps, "revision": 2}
    result = rpc("mobile.snapshot", **scope(opened), limit=1)["result"]
    assert result["todo_state"] == {"todos": steps, "revision": 2}
    session["todo_state"] = {"todos": [], "revision": 3}
    assert rpc("mobile.snapshot", **scope(opened))["result"]["todo_state"] == {"todos": [], "revision": 3}


@pytest.mark.parametrize("canonical_id", [False, True])
def test_canonical_approval_routes_to_real_gateway_queue(mobile_home, peer, monkeypatch, canonical_id):
    from tui_gateway import server_requests as requests
    import tools.approval as approvals
    opened = open_bot()
    entry = SimpleNamespace(data={"request_id": "approval-real", "command": "echo safe", "choices": ["once", "deny"]},
                            event=threading.Event(), result=None)
    monkeypatch.setattr(approvals, "_gateway_queues", {"ops-root": [entry]})
    server._emit_approval_request(opened["session_id"], entry.data)
    request = requests.open_requests(opened["session_id"])[0]
    pending = rpc("mobile.snapshot", **scope(opened))["result"]["pending_approvals"]
    assert [item["request_id"] for item in pending] == ["approval-real"]
    rid = request["id"] if canonical_id else "approval-real"
    assert rpc("mobile.approval.respond", **scope(opened), request_id=rid, choice="deny")["result"] == {"resolved": 1}
    assert entry.event.is_set() and entry.result == "deny"
    # The queue owner withdraws its server request when its wait ends.
    entry.settle("resolved")
    assert requests.open_requests(opened["session_id"]) == []


def test_compute_host_clarification_stays_on_owning_client(mobile_home, peer):
    opened = open_bot()
    sid = opened["session_id"]
    server._sessions[sid]["_compute_host_open_request"] = {
        "id": "srq-child", "method": "clarify", "params": {"session_id": sid, "question": "Remote?"}}
    snapshot = rpc("mobile.snapshot", **scope(opened))["result"]
    assert snapshot["pending_clarify"]["mobile_supported"] is False
    assert rpc("mobile.clarify.respond", **scope(opened), request_id="srq-child", answer="x")["error"]["code"] == 4404


def test_mobile_open_registers_answer_support_only_after_valid_attachment(mobile_home, peer):
    from tui_gateway import server_requests

    assert not server_requests.answers_requests(peer)
    assert "error" in rpc("mobile.open", profile="ops", canonical_root_id="foreign")
    assert not server_requests.answers_requests(peer)
    open_bot()
    assert server_requests.answers_requests(peer)
    server_requests.forget(peer)
    assert not server_requests.answers_requests(peer)
