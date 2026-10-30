"""Family observes cross-process leases without inventing control authority."""
import json
import os
import time

import pytest

from hermes_family import Family, read_profile
from hermes_state import SessionDB


@pytest.mark.parametrize("case,expected", [
    ("live", "working"), ("quiet", "quiet"), ("expired", "unknown"),
    ("dead", "unknown"), ("resident_idle", "unknown"), ("pending", "needs_you"),
    ("terminal", "done"), ("restart", "paused_by_restart"), ("compressed", "working"), ("subagent_compressed", "working"),
    ("reset", "unknown"),
])
def test_family_turn_lease(tmp_path, case, expected):
    path = tmp_path / "state.db"
    now = time.time()
    with SessionDB(path) as db:
        db.create_session("parent", "cli")
        db.create_session("root", "cli")
        tip = "root"
        if case in {"compressed", "subagent_compressed", "reset"}:
            db.end_session("root", "compression")
            tip = "tip"
            db.create_session(tip, "subagent" if case == "subagent_compressed" else "cli",
                              parent_session_id="root",
                              model_config={"_reset_from": "root"} if case == "reset" else None)
        db.record_launch(launch_id="lane", kind="lane", launcher_profile="default",
                         launcher_session_id="parent", child_profile="default")
        db.bind_launch("lane", child_session_id=tip if case in {"reset", "subagent_compressed"} else "root")
        if case == "quiet":
            db._write_sql("UPDATE sessions SET last_activity_at=? WHERE id=?", (now - 1300, tip))
        if case in {"terminal", "restart"}:
            db.finish_launch("lane", "owner_restart" if case == "restart" else "success")
        from hermes_state_pidns import holder_namespace_token
        holder = f"pid={os.getpid()}{holder_namespace_token()}:family-reader"
        if case == "dead":
            holder = f"pid=2147483647{holder_namespace_token()}:family-reader"
        db._write_sql("INSERT INTO session_turn_leases VALUES (?,?,?,?)",
                      ("root", holder, now - 10, now - 1 if case == "expired" else now + 300))
    live = {("default", tip): {"running": False}} if case == "resident_idle" else {}
    if case == "pending":
        live[("default", tip)] = {"pending_kind": "approval"}
    profile = read_profile("default", path, seeds=[("default", "parent")])
    result = Family({"default": profile}, live=live, now=now).session_family("default", "parent")
    node, = result["nodes"]
    assert node["status"] == expected, json.dumps(result)
    assert "stop" not in node["actions"] and "answer" not in node["actions"]
    if case in {"compressed", "subagent_compressed"}:
        # Subagent display-tip policy differs, but the lease must still be read for a subagent tip.
        assert profile.sessions[tip]["turn_leased"] is True
