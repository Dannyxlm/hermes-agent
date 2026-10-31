"""Family shows a thread (a chat the ``thread`` tool opened) by its newest turn.

A thread is hosted by the session owner and never exits, so its launch row never gets a terminal
cause; without this it read ``unknown`` forever after a successful turn.
"""
import time

import pytest

from hermes_family import Family, read_profile
from hermes_state import SessionDB


def _family(path, launch_id, admissions):
    with SessionDB(path) as db:
        db.create_session("parent", "tui")
        db.create_session("child", "tui")
        db.record_launch(launch_id=launch_id, kind="lane", launcher_profile="default",
                         launcher_session_id="parent", child_profile="default")
        db.bind_launch(launch_id, child_session_id="child")
        db.append_message("child", "assistant", "the thread's last word")
        for n, (status, outcome) in enumerate(admissions):
            db._write_sql(
                "INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, "
                "lineage_json, payload_json, payload_digest, intent, status, outcome) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"a{n}", f"thread-{n}", "p", "child", "[]", "{}", "d", "queue", status, outcome))
    profile = read_profile("default", path, seeds=[("default", "parent")])
    node, = Family({"default": profile}, live={}, now=time.time()).session_family("default", "parent")["nodes"]
    return node


@pytest.mark.parametrize("newest,expected", [
    (("terminal", "completed"), "done"), (("terminal", "failed"), "failed"),
    (("terminal", "interrupted"), "stopped"), (("terminal", "cancelled"), "stopped"),
    (("unknown", None), "paused_by_restart"), (("queued", None), "working"),
])
def test_thread_status_follows_its_newest_turn(tmp_path, newest, expected):
    node = _family(tmp_path / "state.db", "thread:k", [("terminal", "completed"), newest])
    assert node["status"] == expected
    assert "resume" not in node["actions"] and "open" in node["actions"]
    if expected in {"done", "failed", "stopped"}:
        assert node["report_hint"] == "the thread's last word"
        assert node["ended_at"] is not None  # finished threads age out like other finished work
    else:
        assert node["report_hint"] is None and node["ended_at"] is None


def test_other_launches_keep_their_own_status(tmp_path):
    node = _family(tmp_path / "state.db", "lane-1", [("terminal", "completed")])
    assert node["status"] == "unknown" and node["report_hint"] is None
