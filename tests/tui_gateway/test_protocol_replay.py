"""Reconnect replay contracts for pending human input."""

import threading
import types

pytest_plugins = ["tests.tui_gateway.test_protocol"]


def test_live_session_payload_replays_pending_approval(server, monkeypatch):
    """A reattached client receives the approval that was emitted while detached."""
    from tools import approval
    from tools import approval_gateway_wait

    session = {
        "agent": types.SimpleNamespace(),
        "cols": 80,
        "created_at": 1.0,
        "history": [],
        "history_lock": threading.Lock(),
        "running": True,
        "session_key": "stored-session",
    }
    first = {
        "choices": ["once", "deny"],
        "command": "rm -rf /tmp/example",
        "description": "recursive delete",
    }
    second = {"command": "rm -rf /tmp/later", "description": "later"}
    saved_queue = approval._gateway_queues.pop("stored-session", None)
    first_entry = approval_gateway_wait._ApprovalEntry(first)
    approval._gateway_queues["stored-session"] = [first_entry, approval_gateway_wait._ApprovalEntry(second)]
    monkeypatch.setattr(server, "_approval_request_payload", lambda data: dict(data or {}))

    try:
        payload = server._live_session_payload("runtime-session", session)
    finally:
        approval._gateway_queues.pop("stored-session", None)
        if saved_queue is not None:
            approval._gateway_queues["stored-session"] = saved_queue

    assert payload["pending_approval"] is not first
    replayed = payload["pending_approval"]
    # request_id is injected by _ApprovalEntry so reconnecting clients can
    # correlate their approval.respond with the exact queued request.
    assert replayed.pop("request_id")
    assert replayed.pop("expires_at") == replayed.pop("created_at") + replayed.pop("timeout_seconds")
    for key in ("scoped_approval_response", "request_epoch", "request_revision"):
        assert replayed.pop(key) == first_entry.data[key]
    assert replayed == first


def test_live_session_payload_replays_open_requests(server):
    """A reattached client receives the server→client request still blocking the session (a clarify sent
    while detached), scoped to the owning runtime session only, as a snapshot not a live reference."""
    from tui_gateway import server_requests
    session = {
        "agent": types.SimpleNamespace(), "cols": 80, "created_at": 1.0, "history": [],
        "history_lock": threading.Lock(), "running": True, "session_key": "stored-session",
    }
    req = server_requests.ServerRequest("runtime-session", "clarify", {"question": "Which?", "choices": ["a", "b"]})
    server_requests._open[req.id] = req
    try:
        payload = server._live_session_payload("runtime-session", session)
        other = server._live_session_payload("other-session", session)
    finally:
        server_requests._open.pop(req.id, None)

    replay = payload["open_requests"][0]
    assert replay["id"] == req.id and replay["method"] == "clarify"
    assert replay["params"]["session_id"] == "runtime-session"
    assert replay["params"]["question"] == "Which?"
    assert replay["params"]["choices"] == ["a", "b"]
    assert replay["params"]["expires_at"] is None
    assert replay["params"]["request_epoch"] == server_requests.inbox_epoch()
    assert payload["open_requests"][0]["params"] is not req.params
    assert "open_requests" not in other
