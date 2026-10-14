"""Attention ordering, dispatch freshness, privacy and compute-owner regressions."""
import json
import threading
import time

import pytest

from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer, rpc  # noqa: PLC0414 - imported pytest fixtures
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service  # noqa: PLC0414 - imported pytest fixtures
from tests.tui_gateway.test_human_input_push import enrolled as enrolled, ask, pending_jobs  # noqa: PLC0414 - imported pytest fixtures
from tui_gateway import server, server_requests
from tui_gateway.mobile_push_payloads import AlertPreview, Scope, activity_payload
from tui_gateway.mobile_push_provider import DeliveryResult


def test_compute_child_never_instantiates_a_second_push_worker(monkeypatch):
    from tui_gateway import mobile_push
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    def forbidden(_home):
        pytest.fail("compute child attempted to start a second push service")
    monkeypatch.setattr(mobile_push, "service_for_home", forbidden)
    assert server._mobile_push_service() is None


def test_preview_and_qid_always_describe_same_authoritative_question(enrolled, push_service):
    sid, _, _ = enrolled
    first = ask(sid, "clarify", {"questions": [
        {"qid": "a", "question": "First?"}, {"qid": "b", "question": "Second?"}]})
    ask(sid, "clarify", {"questions": [{"qid": "c", "question": "Different request?"}]})
    payload = json.loads(pending_jobs(push_service)[-1]["payload"])
    assert payload["aps"]["alert"]["body"] == "First?"
    assert payload["hermex.destination"]["human_input"]["question_id"] == "a"
    server_requests.lock_answer(first["id"], "a", "LOCKED_SENTINEL")
    payload = json.loads(pending_jobs(push_service)[-1]["payload"])
    assert payload["aps"]["alert"]["body"] == "Second?"
    assert payload["hermex.destination"]["human_input"]["question_id"] == "b"
    assert "LOCKED_SENTINEL" not in json.dumps(payload)


def test_partial_progress_after_settlement_cannot_reopen_request(enrolled, push_service, monkeypatch):
    sid, scope, _ = enrolled
    frame = ask(sid, "clarify", {"questions": [{"qid": "a", "question": "A?"}, {"qid": "b", "question": "B?"}]})
    entered, release = threading.Event(), threading.Event()
    original = server_requests._attention_changed
    def pause(req):
        if threading.current_thread().name == "partial-lock":
            entered.set()
            assert release.wait(10)
        original(req)
    monkeypatch.setattr(server_requests, "_attention_changed", pause)
    errors = []
    def locking():
        try:
            server_requests.lock_answer(frame["id"], "a", "dummy")
        except Exception as exc:
            errors.append(type(exc).__name__)
    worker = threading.Thread(target=locking, name="partial-lock")
    worker.start()
    try:
        assert entered.wait(10)
        server_requests.cancel(sid)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    assert not server_requests.open_requests(sid)
    assert not pending_jobs(push_service)
    assert push_service.current_run(scope)["status"] == "thinking"


def test_projection_cut_cannot_commit_after_newer_cancellation(enrolled, push_service, monkeypatch):
    from tui_gateway import mobile_push_attention as attention
    sid, scope, _ = enrolled
    frame = ask(sid, "vault.code")
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original = attention.pending_projection
    def paused(*args):
        result = original(*args)
        if threading.current_thread().name == "old-projection":
            entered.set()
            assert release.wait(10)
        return result
    monkeypatch.setattr(attention, "pending_projection", paused)
    old = threading.Thread(target=lambda: server._mobile_push_capture(frame), name="old-projection")
    def cancel():
        server_requests.cancel(sid)
        cancelled.set()
    newer = threading.Thread(target=cancel)
    old.start()
    try:
        assert entered.wait(10)
        newer.start()
    finally:
        release.set()
        old.join(10)
        newer.join(10)
    assert cancelled.is_set() and not old.is_alive()
    assert pending_jobs(push_service) == []
    assert push_service.current_run(scope)["status"] == "thinking"


def test_compute_mirror_secure_legacy_and_partial_update_are_single_projection(enrolled, push_service):
    sid, scope, _ = enrolled
    params = {"session_id": sid, "created_at": time.time(), "expires_at": time.time() + 300,
              "request_epoch": "child-epoch", "request_revision": 1,
              "questions": [{"qid": "a", "question": "A?"}, {"qid": "b", "question": "B?"}]}
    frame = {"jsonrpc": "2.0", "id": "srq-child", "method": "clarify", "params": params}
    server._relay_compute_host_rpc(frame)
    server._relay_compute_host_rpc(frame)
    assert len(pending_jobs(push_service)) == 1
    update = {"id": "srq-child", "method": "clarify", "request_epoch": "child-epoch",
              "request_revision": 2, "remaining_question_ids": ["b"]}
    server._relay_compute_host_rpc(server._event_frame("request.updated", sid, update))
    payload = json.loads(pending_jobs(push_service)[-1]["payload"])
    assert payload["hermex.destination"]["human_input"]["question_id"] == "b"
    assert payload["aps"]["alert"]["body"] == "B?"
    server._relay_compute_host_rpc(server._event_frame("request.cancel", sid,
        {"id": "srq-child", "method": "clarify", "reason": "resolved"}))
    server._relay_compute_host_rpc(server._event_frame("request.updated", sid, update))
    assert not pending_jobs(push_service)
    server._relay_compute_host_rpc(frame)  # delayed original request cannot re-notify a retired id
    assert not pending_jobs(push_service)
    legacy = {"id": "srq-legacy", "method": "vault.code", "params": {"session_id": sid, "hint": "SECRET_SENTINEL"}}
    server._relay_compute_host_rpc(legacy)
    payload = json.loads(pending_jobs(push_service)[-1]["payload"])
    assert payload["hermex.destination"]["human_input"]["allowed_actions"] == ["open_securely"]
    assert "request_epoch" not in payload["hermex.destination"]["human_input"]
    assert "SECRET_SENTINEL" not in json.dumps(payload)
    assert server_requests.attention_summaries([sid])[sid]["count"] == 1
    assert push_service.current_run(scope)["status"] == "waitingForClarification"


def test_nearest_deadline_bounds_activity_freshness_and_apns_expiration(enrolled, push_service):
    sid, scope, ids = enrolled
    first = ask(sid, "vault.code")
    run_id = server._sessions[sid]["_mobile_push_run_id"]
    push_service.store.register("owner", scope, installation_id=ids["installation_id"], connection_id=ids["connection_id"],
        kind="activity", token="cd" * 32, environment="production", activity_id="activity", run_id=run_id)
    row = pending_jobs(push_service, "activity")[-1]
    aps = json.loads(row["payload"])["aps"]
    assert aps["content-state"]["pendingInput"]["request_id"] == first["id"]
    assert aps["content-state"]["pendingCount"] == 1
    assert aps["stale-date"] <= first["params"]["expires_at"]
    # Earlier expiry belongs to another request, not the FIFO primary.
    preview = AlertPreview(attention={"count": 2, "primary": {"method": "clarify", "category": "HERMEX_QUESTION_V1", "expires_at": 1000},
                                     "nearest_expires_at": 101})
    payload = activity_payload({"status": "waitingForClarification", "started_at": 50, "updated_at": 100}, scope, 100, preview=preview)
    assert payload["aps"]["stale-date"] == 101
    push_service.store.clock = lambda: first["params"]["expires_at"] + 1
    assert push_service.store.claim() is None


def test_preview_narrowing_and_logout_apply_at_dispatch(enrolled, push_service):
    sid, _, ids = enrolled
    ask(sid, "clarify", {"questions": [{"qid": "q", "question": "PRIVATE_QUESTION_SENTINEL"}]})
    refresh = {k: ids[k] for k in ("installation_id", "connection_id", "device_token", "environment")}
    assert rpc("mobile.push.refresh", **refresh, preview_enabled=False)["result"]["updated"] == 1
    job = push_service.store.claim()
    assert "PRIVATE_QUESTION_SENTINEL" not in json.dumps(job["payload"])
    push_service.store.finish(job, DeliveryResult("retry", "fixture"))
    assert rpc("mobile.push.unregister", **{k: ids[k] for k in ("installation_id", "connection_id")})["result"]["removed"] == 1
    assert not pending_jobs(push_service)


def test_failed_projection_does_not_expose_exception_or_stop_request(enrolled, push_service, monkeypatch, caplog):
    sid, _, _ = enrolled
    def fail(*args, **kwargs):
        raise OSError("SECRET_STORAGE_SENTINEL")
    monkeypatch.setattr(push_service.store, "record", fail)
    frame = ask(sid, "secret", {"env_var": "DUMMY", "prompt": "Dummy prompt"})
    assert server_requests.open_requests(sid)[0]["id"] == frame["id"]
    assert "SECRET_STORAGE_SENTINEL" not in caplog.text


@pytest.mark.parametrize("params,expected", [
    ({"tool_name": "read_file", "kind": "tool", "risk": "read_only", "choices": ["once", "deny"]}, {"open", "once", "deny", "suggest"}),
    ({"tool_name": "terminal", "choices": ["once", "deny"]}, {"open", "deny", "suggest"}),
    ({"tool_name": "read_file", "surface": "payment", "choices": ["once", "deny"]}, {"open", "deny", "suggest"}),
    ({"tool_name": "read_file", "choices": ["session", "always"]}, {"open"}),
])
def test_quick_actions_never_grant_unoffered_or_broad_scope(enrolled, push_service, params, expected):
    sid, _, _ = enrolled
    ask(sid, "approval", {"request_id": "queue-test", "command": "private", **params})
    target = json.loads(pending_jobs(push_service)[-1]["payload"])["hermex.destination"]["human_input"]
    assert set(target["allowed_actions"]) == expected
