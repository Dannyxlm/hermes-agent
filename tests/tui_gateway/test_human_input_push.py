"""Real native producer → registry → private outbox; no APNs or real credentials."""
import json

import pytest

from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer, open_bot, rpc  # noqa: PLC0414 - imported pytest fixtures
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service, registration  # noqa: PLC0414 - imported pytest fixtures
from tui_gateway import server, server_requests
from tui_gateway.mobile_push_payloads import Scope


@pytest.fixture
def enrolled(push_service, peer):
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    opened = open_bot()
    params = registration(opened)
    assert "result" in rpc("mobile.push.register", **params, preview_enabled=True)
    sid = opened["session_id"]
    server._emit("message.start", sid)
    server._sessions[sid]["transport"] = server._detached_ws_transport
    yield sid, Scope("native", opened["profile"], opened["canonical_root_id"]), params
    server_requests.cancel(sid)


def pending_jobs(service, kind="alert"):
    return [dict(row) for row in service.store._db.execute(
        "SELECT o.* FROM outbox o JOIN subscriptions s ON s.id=o.subscription_id "
        "WHERE o.state='pending' AND s.kind=? ORDER BY o.rowid", (kind,))]


def ask(sid, method, params=None):
    server_requests.send_async(method, sid, params or {}, lambda result: None, timeout=300)
    return server_requests.open_requests(sid)[-1]


@pytest.mark.parametrize("method,params,category", [
    ("approval", {"choices": ["once", "deny"], "command": "private-command", "tool_name": "read_file"}, "HERMEX_APPROVAL_V1"),
    ("clarify", {"questions": [{"qid": "q1", "question": "Question?"}]}, "HERMEX_QUESTION_V1"),
    ("setup_choose", {"kind": "question", "question": "Private setup"}, "HERMEX_SETUP_V1"),
    ("sudo", {"command": "SENSITIVE_SENTINEL"}, "HERMEX_SECURE_INPUT_V1"),
    ("secret", {"env_var": "SENSITIVE_SENTINEL", "prompt": "SENSITIVE_SENTINEL"}, "HERMEX_SECURE_INPUT_V1"),
    ("vault.unlock_prompt", {"backend": "SENSITIVE_SENTINEL", "display_name": "SENSITIVE_SENTINEL"}, "HERMEX_SECURE_INPUT_V1"),
    ("vault.save_login", {"site": "SENSITIVE_SENTINEL", "origin": "SENSITIVE_SENTINEL"}, "HERMEX_SECURE_INPUT_V1"),
    ("vault.code", {"site": "SENSITIVE_SENTINEL", "hint": "SENSITIVE_SENTINEL"}, "HERMEX_SECURE_INPUT_V1"),
])
def test_all_families_project_safe_scoped_category(enrolled, push_service, method, params, category):
    sid, _scope, _ = enrolled
    frame = ask(sid, method, params)
    job = pending_jobs(push_service)[-1]
    payload = json.loads(job["payload"])
    assert payload["aps"]["category"] == category
    target = payload["hermex.destination"]["human_input"]
    assert target["request_id"] == frame["id"]
    assert target["session_id"] == sid
    assert target["request_epoch"] == frame["params"]["request_epoch"]
    assert target["request_revision"] == 1
    assert job["expires_at"] <= frame["params"]["expires_at"]
    stored = " ".join(str(tuple(row)) for table in ("outbox", "run_state")
                      for row in push_service.store._db.execute(f"SELECT * FROM {table}"))
    assert "SENSITIVE_SENTINEL" not in stored
    assert "private-command" not in stored
    if category == "HERMEX_SECURE_INPUT_V1":
        assert target["allowed_actions"] == ["open_securely"]
    assert server_requests.attention_summaries([sid])[sid]["count"] == 1


def test_settlement_replaces_exact_descriptor_and_drops_leased_retry(enrolled, push_service):
    sid, scope, _ = enrolled
    first = ask(sid, "clarify", {"questions": [{"qid": "q1", "question": "One?"}]})
    second = ask(sid, "vault.code")
    job = push_service.store.claim()
    assert job is not None
    assert server_requests.resolve_response({"id": first["id"], "result": {"answers": {"q1": "yes"}}})
    remaining = pending_jobs(push_service)
    assert all(row["job_id"] != job["job_id"] for row in remaining)
    target = json.loads(remaining[-1]["payload"])["hermex.destination"]["human_input"]
    assert target["request_id"] == second["id"]
    assert json.loads(remaining[-1]["payload"])["hermex.pending_count"] == 1
    assert push_service.current_run(scope)["status"] == "waitingForClarification"


def test_partial_locks_refresh_qid_revision_without_answer_persistence(enrolled, push_service):
    sid, _, _ = enrolled
    frame = ask(sid, "clarify", {"questions": [
        {"qid": "q1", "question": "First?"}, {"qid": "q2", "question": "Second?"}]})
    assert server_requests.lock_answer(frame["id"], "q1", "ANSWER_SENTINEL") == ["q2"]
    row = pending_jobs(push_service)[-1]
    target = json.loads(row["payload"])["hermex.destination"]["human_input"]
    assert target["question_id"] == "q2"
    assert target["request_revision"] == 2
    assert "ANSWER_SENTINEL" not in row["payload"]
    assert "reply" in target["allowed_actions"]


def test_sessionless_install_is_connection_owned_and_only_enrolled_profile_notified(enrolled, push_service):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    sid, scope, ids = enrolled
    # Same device enrolled twice in this profile gets one connection alert; another
    # profile's attention lease must not receive this host prompt.
    from tui_gateway.methods_browser_control import _principal_digest
    principal = _principal_digest({"user_id": "test-user", "provider": "test-auth"})
    for profile, root_id in ((scope.profile, "other-chat"), ("default", "foreign-chat")):
        push_service.store.register(principal, Scope("native", profile, root_id),
            installation_id=ids["installation_id"], connection_id=ids["connection_id"],
            token="ef" * 32, environment="production")
    token = set_hermes_home_override(server._sessions[sid]["profile_home"])
    try:
        frame = ask("", "display.install.sudo", {"profile_key": "PRIVATE_HOST_SENTINEL"})
    finally:
        reset_hermes_home_override(token)
    try:
        assert len(pending_jobs(push_service)) == 1
        job = pending_jobs(push_service)[-1]
        target = json.loads(job["payload"])["hermex.destination"]
        assert target["surface"] == "connection"
        assert target["profile"] == scope.profile
        assert "canonical_root_id" not in target and "session_id" not in target
        assert target["human_input"]["session_id"] == ""
        assert target["human_input"]["request_id"] == frame["id"]
        assert "PRIVATE_HOST_SENTINEL" not in job["payload"]
        assert server_requests.resolve_response({"id": frame["id"], "result": {"value": ""}})
        assert pending_jobs(push_service) == []
    finally:
        server_requests.cancel("")


def test_connector_sequence_and_settlement_are_attention_not_completion(enrolled, push_service):
    sid, scope, _ = enrolled
    import time
    body = {"op_id": "op-fixture", "seq": 1, "deadline_at": time.time() + 300,
            "timeout_seconds": 300, "targets": [{"name": "SENSITIVE_SENTINEL", "kind": "mcp", "action": "install",
            "state": "needs_input", "required_env": [{"name": "TOKEN", "secret": True, "default": "SENSITIVE_SENTINEL"}]}]}
    # Native envelope: projection must not persist any target or credential metadata.
    server._mobile_push_capture({"method": "event", "params": {"session_id": sid, "type": "connection.request", "payload": body}})
    row = pending_jobs(push_service)[-1]
    payload = json.loads(row["payload"])
    assert payload["aps"]["category"] == "HERMEX_SECURE_INPUT_V1"
    assert "SENSITIVE_SENTINEL" not in row["payload"]
    assert payload["hermex.destination"]["human_input"]["operation_id"] == "op-fixture"
    body.update(seq=2, settled=True)
    server._mobile_push_capture({"method": "event", "params": {"session_id": sid, "type": "connection.update", "payload": body}})
    body.update(seq=1, settled=False)
    server._mobile_push_capture({"method": "event", "params": {"session_id": sid, "type": "connection.request", "payload": body}})
    assert push_service.current_run(scope)["status"] == "thinking"
    assert pending_jobs(push_service) == []
