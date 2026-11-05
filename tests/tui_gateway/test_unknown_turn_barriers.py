"""Unknown outcomes are barriers, not permission to forward or recover a turn."""
import os

import pytest

from hermes_cli.active_sessions import ActiveSessionRefusal, SESSION_NOT_OWNED
from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, begin_runtime_epoch, claim_session_input, recover_session_inputs,
    register_worker_execution,
)
from tests.tui_gateway.test_auto_continue import _session
from tests.tui_gateway.test_legacy_submit_forwards_to_owner import _legacy
from tui_gateway import owner_forward, server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

pytest_plugins = ["tests.tui_gateway.test_auto_continue"]


@pytest.mark.parametrize("unknown_table", ["admission", "worker"])
def test_unknown_barrier_dominates_queued_followers(monkeypatch, tmp_path, unknown_table):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    forwarded = []
    monkeypatch.setattr(owner_forward, "forward_prompt", lambda *a: forwarded.append(a) or {})
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="first")
        if unknown_table == "admission":
            admit_session_input(db, epoch=epoch, principal_id="owner", session_id=key,
                                request_id="lost", payload={"text": "may have run"})
            claim_session_input(db, epoch=epoch, session_id=key)
        else:
            register_worker_execution(db, epoch=epoch, execution_id="lost-worker", session_id=key,
                                      generation=0, kind="compute", adoption_secret="fixture-only")
        epoch = begin_runtime_epoch(db, instance_id="replacement")
        recover_session_inputs(db, epoch=epoch)
        admit_session_input(db, epoch=epoch, principal_id="owner", session_id=key,
                            request_id="follower", payload={"text": "queued behind unknown"})
        refusal = server._canonical_ledger_refusal(server._sessions[sid])
        assert refusal.ledger_status == "unknown"
        reply = server.handle_request({"id": "send", "method": "prompt.submit",
            "params": {"session_id": sid, "text": "another follower"}})
        assert reply["error"]["data"] == {"reason": SESSION_NOT_OWNED, "ownership_reason": "outcome_unknown"}
        assert forwarded == [] and turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_unavailable_ledger_owner_has_distinct_rpc_reason(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    def unavailable(home):
        raise owner_forward.OwnerNotRunning("no ready owner")
    monkeypatch.setattr(owner_forward, "discover_owner", unavailable)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="first")
        admit_session_input(db, epoch=epoch, principal_id="owner", session_id=key,
                            request_id="queued", payload={"text": "waiting"})
        reply = server.handle_request({"id": "send", "method": "prompt.submit",
            "params": {"session_id": sid, "text": "do not run here"}})
        assert reply["error"]["data"] == {"reason": SESSION_NOT_OWNED, "ownership_reason": "owner_unavailable"}
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_foreign_live_lease_has_distinct_rpc_reason(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, _key, turns, _emitted = _legacy(monkeypatch, db)
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *a, **kw:
                        (None, ActiveSessionRefusal("Session already has a live owner", SESSION_NOT_OWNED)))
    try:
        reply = server.handle_request({"id": "send", "method": "prompt.submit",
            "params": {"session_id": sid, "text": "not this writer"}})
        assert reply["error"]["data"] == {"reason": SESSION_NOT_OWNED, "ownership_reason": "owner_live_elsewhere"}
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


@pytest.mark.parametrize("writer_state", ["unknown", "alive", "dead"])
def test_foreign_recovery_requires_proven_death(monkeypatch, emits, marker_home, schedule_env, writer_state):
    record_turn_start(marker_home, "session-key", "do not replay uncertain work")
    marker = read_turn_marker(marker_home, "session-key")
    marker["writer_pid"] = os.getpid() + 1000000
    monkeypatch.setattr(server, "read_turn_marker", lambda *a: marker)
    monkeypatch.setattr(server, "marker_writer_state", lambda entry: writer_state)
    session = _session()
    result = server._maybe_schedule_auto_continue("sid", session, "session-key")
    assert (result is not None) == (writer_state == "dead")
    assert len(schedule_env) == (1 if writer_state == "dead" else 0)
    if writer_state != "dead":
        assert read_turn_marker(marker_home, "session-key") is not None
        assert not emits and not session.get("_auto_continue_scheduled")
