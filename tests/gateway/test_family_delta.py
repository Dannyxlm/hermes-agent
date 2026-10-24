"""Family keeps logical control ownership when compression advances the transcript."""
import threading
from types import SimpleNamespace

import pytest

from gateway.session_authority import LiveSession
from tests.gateway.test_family_review import reader, family
from tests.test_hermes_family import launch, NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["root", "tip"])
@pytest.mark.parametrize("kind", ["approval", "clarify"])
async def test_compressed_pending_uses_logical_claim(reader, requested, kind):
    db, authority, connection = reader
    from hermes_state_runtime import admit_session_input, claim_session_input
    db._conn.execute("UPDATE sessions SET ended_at=1,end_reason='compression',runtime_generation=4 WHERE id='root'")
    db.create_session("tip", source="test", parent_session_id="root")
    db._conn.execute("UPDATE sessions SET runtime_generation=4 WHERE id='tip'")
    db._conn.commit()
    admit_session_input(db, epoch=authority.epoch, principal_id="owner", session_id="root",
                        request_id="turn", payload={"text": "post-compression turn"})
    claim = claim_session_input(db, epoch=authority.epoch, session_id="root")
    assert claim["generation"] == 5
    assert db.get_session("tip")["runtime_generation"] == 4
    live = LiveSession(None, "route")
    authority.sessions["root"] = live
    live.controls.pending["question"] = (SimpleNamespace(event=threading.Event()),
        {"kind": kind, "prompt_id": "question", "execution_generation": 5})
    live.controls.remote_responders["question"] = object()
    try:
        result = await family(connection, requested)
        assert "result" in result, result
        root = result["result"]["root"]
        assert root["session_id"] == "tip"
        assert root["node_id"] == '["default","session","tip"]'
        assert root["root_session_id"] == "tip"
        assert root["status"] == "needs_you", root
        assert root["needs_you"] == {"kind": kind, "session_id": "root", "profile": "default"}
        assert "answer" in root["actions"]
        live.controls.pending["question"][1]["execution_generation"] = 4
        stale = await family(connection, requested)
        assert stale["result"]["root"]["needs_you"] is None
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,visible", [("started", False), ("terminal", True)])
async def test_compressed_failed_helper_uses_logical_admission(reader, status, visible):
    db, authority, connection = reader
    db._conn.execute("UPDATE sessions SET ended_at=1,end_reason='compression' WHERE id='root'")
    db.create_session("tip", source="test", parent_session_id="root")
    db.create_session("helper", source="subagent", parent_session_id="tip")
    launch(db._conn, "helper", launcher="tip", cause="error", finished=NOW)
    db._conn.commit()
    from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_input
    admit_session_input(db, epoch=authority.epoch, principal_id="owner", session_id="root",
                        request_id="turn", payload={"text": "post-compression turn"})
    claim = claim_session_input(db, epoch=authority.epoch, session_id="root")
    if status == "terminal":
        settle_session_input(db, epoch=authority.epoch, admission_id=claim["admission_id"],
                             generation=claim["generation"], outcome="completed")
    authority.sessions["root"] = LiveSession(None, "route")
    try:
        response = await connection.dispatch({"id": 2, "method": "family.overview", "params": {"since": 0}})
        assert "result" in response, response
        assert bool(response["result"]["failed"]) is visible
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_family_never_authorizes_from_worker(reader, monkeypatch):
    db, authority, connection = reader
    authority.sessions["root"] = LiveSession(None, "route")
    loop_thread = threading.get_ident()
    calls = []
    original = authority.authorize
    def checked(*args):
        calls.append(threading.get_ident())
        assert threading.get_ident() == loop_thread, "Family may restore an evicted session off-loop"
        return original(*args)
    monkeypatch.setattr(authority, "authorize", checked)
    before = db._conn.total_changes
    try:
        response = await family(connection)
        assert "result" in response, response
        assert calls and all(t == loop_thread for t in calls)
        assert db._conn.total_changes == before
    finally:
        await connection.close()
