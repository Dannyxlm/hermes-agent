"""Family polling keeps compression ownership separate from resident stop ownership."""
import json
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session_authority import LiveSession
from tests.test_hermes_family import launch

pytest_plugins = ["tests.gateway.test_family_review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["session.family", "family.overview"])
@pytest.mark.parametrize("can_control", [False, True])
async def test_multiple_residents_keep_compressed_controls_scoped(reader, monkeypatch, method, can_control):
    db, authority, connection = reader
    home = authority.profile_id
    if not can_control:
        connection.actor = replace(connection.actor, capabilities=frozenset({"session:read", "session:respond"}))
    db.create_session("compressed", source="test")
    db._conn.execute("UPDATE sessions SET end_reason='compression',ended_at=1,runtime_generation=7 WHERE id='compressed'")
    db._conn.commit()
    db.create_session("tip", source="test", parent_session_id="compressed")
    db.create_session("private", source="local", user_id="somebody-else")
    launch(db._conn, "compressed", kind="teammate")
    launch(db._conn, "private", kind="teammate")
    db._conn.commit()
    db.set_meta("gateway.local_policy.v1:logical", json.dumps({
        "entry": {"session_id": "tip"}, "principal_id": connection.actor.subject}))

    root_agent = SimpleNamespace(session_id="root")
    private_agent = SimpleNamespace(session_id="private")
    agents = {"root-route": root_agent, "private-route": private_agent}
    entries = {route: SimpleNamespace(session_id=agent.session_id) for route, agent in agents.items()}
    authority.runner.session_store = SimpleNamespace(_lock=threading.Lock(), _entries=entries)
    authority.runner._resident_agent_for = agents.get
    # The first handle authorizes a stop context; the second needs the compression-owner fallback.
    authority.sessions["root"] = LiveSession(None, "root-route")
    pending = LiveSession(None, "compressed-route")
    authority.sessions["logical"] = pending
    authority.sessions["private"] = LiveSession(
        SimpleNamespace(platform=Platform.LOCAL, user_id="somebody-else"), "private-route")
    pending.controls.pending["question"] = (SimpleNamespace(event=threading.Event()), {
        "kind": "clarify", "prompt_id": "question", "execution_generation": 7})

    processes = {}
    for pid, profile_home, route, owner in (
        ("owned", home, "root-route", "root"),
        ("wrong-owner", home, "root-route", "unrelated"),
        ("wrong-route", home, "unrelated-route", "root"),
        ("wrong-profile", str(db.db_path.parent / "other"), "root-route", "root"),
        ("private-process", home, "private-route", "private"),
    ):
        launch(db._conn, pid, kind="process", backend=pid)
        processes[pid] = SimpleNamespace(id=pid, owner_profile_home=profile_home,
            session_key=route, owner_task_id=owner, task_id=owner, exited=False, detached=False,
            _scope_stop_pending=False, exit_code=None, command="fixture command", exited_at=None,
            _heartbeat_last=None)
    db._conn.commit()
    monkeypatch.delitem(sys.modules, "tools.delegate_tool_registry", raising=False)
    monkeypatch.setitem(sys.modules, "tools.process_registry", SimpleNamespace(process_registry=SimpleNamespace(
        _lock=threading.Lock(), _running=processes, _finished={})))
    before = db._conn.total_changes
    params = {"profile": "default", "session_id": "root", "since": 0} if method == "session.family" else {"since": 0}
    try:
        response = await connection.dispatch({"id": 1, "method": method, "params": params})
        assert "result" in response, response
        result = response["result"]
        if method == "session.family":
            nodes = result["nodes"]
        else:
            nodes = [node for root in result["roots"] for node in root["live_leaves"]]
        by_title = {node["title"]: node for node in nodes}
        compressed = by_title["compressed"]
        assert compressed["session_id"] == "tip"
        assert compressed["status"] == "needs_you"
        assert compressed["needs_you"] == {"kind": "clarify", "session_id": "logical", "profile": "default"}
        assert "answer" in compressed["actions"]
        assert ("stop" in by_title["owned"]["actions"]) is can_control
        for pid in ("wrong-owner", "wrong-route", "private-process"):
            assert "stop" not in by_title[pid]["actions"]
        if method == "session.family":
            assert "stop" not in by_title["wrong-profile"]["actions"]
        else:
            assert "wrong-profile" not in by_title
        assert "private" not in by_title
        assert db._conn.total_changes == before
        assert set(authority.sessions) == {"root", "logical", "private"}
    finally:
        await connection.close()
