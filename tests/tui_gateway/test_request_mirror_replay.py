"""Live-only vault correlation survives without replaying scripts or breaking phone attention."""

import json
import threading

import pytest

from tui_gateway import mobile_push_attention as attention
from tui_gateway import server as srv
from tui_gateway import server_requests as requests


@pytest.fixture
def mirrored_session(monkeypatch):
    sid = "mirror-replay-union"
    session = {"history_lock": threading.Lock()}
    monkeypatch.setitem(srv._sessions, sid, session)
    monkeypatch.setattr(srv, "write_json", lambda frame: True)
    requests.reset_for_tests()
    yield sid, session
    requests.reset_for_tests()


def _question(sid):
    return {"jsonrpc": "2.0", "id": "srq-question", "method": "clarify", "params": {
        "session_id": sid, "request_epoch": "child-epoch", "request_revision": 1,
        "created_at": 1.0, "expires_at": None, "questions": [{"qid": "q", "question": "Which color?"}]}}


def test_mixed_child_requests_replay_only_the_question_but_keep_vault_control(mirrored_session):
    sid, session = mirrored_session
    question = _question(sid)
    secret = "synthetic-secret-not-in-a-mirror"
    vault = {"jsonrpc": "2.0", "id": "srq-vault", "method": "preview.act", "params": {
        "session_id": sid, "action": "vault", "vault": {
            "operation": "evaluate", "target": "pinned-tab", "expression": secret}}}
    assert srv._relay_compute_host_rpc(question)
    class Window:
        def write(self, frame):
            return True
    opener = {"id": "open", "method": "preview.act", "params": {
        "session_id": sid, "action": "vault", "vault": {"operation": "open"}}}
    requests.preview_request_transport(opener)
    requests.remember_preview_response({"id": "open", "result": {"value": json.dumps({
        "success": True, "target": "pinned-tab"})}}, Window())
    assert srv._relay_compute_host_rpc(vault)
    mirrors = session["_compute_host_open_requests"]
    assert mirrors["srq-vault"] == {"id": "srq-vault", "method": "preview.act", "replayable": False}
    assert secret not in json.dumps(mirrors)
    assert [frame["id"] for frame in srv._open_requests(sid)] == ["srq-question"]
    assert list(attention.request_frames(session, sid)) == ["srq-question"]
    targets = attention.pending_projection(session, sid, now=2.0)
    assert [target["request_id"] for target in targets] == ["srq-question"]
    assert attention.input_preview(session, sid, attention.attention_state(targets)) == "Which color?"
    # Presentation must not erase control correlation: the bridge still finds the owning child by id.
    assert srv._compute_host_request_session("srq-vault") == (sid, session)
    mirrors.pop("srq-question")
    assert srv._open_requests(sid) == []
    assert attention.pending_projection(session, sid, now=2.0) == []


def test_legacy_vault_mirror_is_filtered_beside_an_ordinary_request(mirrored_session):
    sid, session = mirrored_session
    question = _question(sid)
    session["_compute_host_open_requests"] = {question["id"]: question}
    session["_compute_host_open_request"] = {"id": "srq-legacy-vault", "method": "preview.act", "replayable": False}
    assert [frame["id"] for frame in srv._open_requests(sid)] == [question["id"]]
    assert list(attention.request_frames(session, sid)) == [question["id"]]
    assert [target["request_id"] for target in attention.pending_projection(session, sid, now=2.0)] == [question["id"]]
    session.pop("_compute_host_open_requests")
    assert srv._open_requests(sid) == []
    assert attention.pending_projection(session, sid, now=2.0) == []


def test_isolated_vault_followups_and_cancellation_never_fan_out(mirrored_session, monkeypatch):
    from types import SimpleNamespace
    from tui_gateway.transport import bind_transport, reset_transport
    sid, session = mirrored_session
    broadcast, owner_frames, forwarded = [], [], []
    owner = SimpleNamespace(write=lambda frame: owner_frames.append(frame) or True)
    phone = SimpleNamespace(write=lambda frame: pytest.fail("phone received a vault frame"))
    monkeypatch.setattr(srv, "write_json", lambda frame: broadcast.append(frame) or True)
    monkeypatch.setattr(srv, "_session_uses_compute_host", lambda session: True)
    def respond(sid, payload, **kwargs):
        forwarded.append(payload)
        return {"type": "respond.ack", "response": {"result": {"status": "ok"}}}
    monkeypatch.setattr(srv, "_get_compute_host_supervisor", lambda: SimpleNamespace(respond=respond))

    for operation in ("open", "evaluate", "close"):
        vault = {"operation": operation, "target": "target"}
        if operation == "evaluate":
            vault["expression"] = "synthetic-secret"
        frame = {"id": operation, "method": "preview.act", "params": {
            "session_id": sid, "action": "vault", "vault": vault}}
        assert srv._relay_compute_host_rpc(frame)
        response = {"id": operation, "result": {"value": json.dumps({"success": True, "target": "target"})}}
        if operation != "open":
            count = len(forwarded)
            token = bind_transport(phone)
            try:
                assert srv._relay_compute_host_response(response)
            finally:
                reset_transport(token)
            assert len(forwarded) == count
        token = bind_transport(owner)
        try:
            assert srv._relay_compute_host_response(response)
        finally:
            reset_transport(token)
        assert srv._relay_compute_host_rpc({"method": "event", "params": {
            "type": "request.cancel", "session_id": sid, "payload": {"id": operation}}})
    assert [f["id"] for f in broadcast if "id" in f] == ["open"]
    assert [f["id"] for f in owner_frames if "id" in f] == ["evaluate", "close"]
    assert [f["params"]["payload"]["id"] for f in owner_frames if f["method"] == "event"] == ["evaluate", "close"]