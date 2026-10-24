"""Review regressions for bounded Family reads and compression continuations."""
import sqlite3

import pytest

from hermes_family import Family, read_profile
from tests.test_hermes_family import NOW, fixture_db, launch, session


def test_lane_hint_plan_is_driven_by_launches(tmp_path):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root")
        session(db, "lane")
        launch(db, "lane", kind="lane", cause="success", finished=NOW)
        db.executescript("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT);"
                         "CREATE INDEX idx_messages_session_id ON messages(session_id);")
        db.execute("INSERT INTO messages VALUES(1,'lane','assistant','Final reply',NULL)")
    statements = []
    snapshot = read_profile("default", path, trace=statements.append)
    assert snapshot.launches[0]["report_hint"] == "Final reply"
    with sqlite3.connect(path) as db:
        plans = [r[3] for sql in statements if "FROM messages" in sql
                 for r in db.execute("EXPLAIN QUERY PLAN " + sql)]
    assert plans, statements
    assert not any("SCAN m" in plan for plan in plans), plans
    assert any("idx_messages_session_id" in plan for plan in plans), plans


def test_pre_update_helpers_stay_unlinked(tmp_path):
    path = tmp_path / "state.db"
    with fixture_db(path, old=True) as db:
        session(db, "root", source="desktop")
        for i in range(8):
            session(db, f"legacy-{i}", source="subagent", parent="root", ended=NOW - 90 * 86400)
    result = Family({"default": read_profile("default", path)}, now=NOW).session_family(
        "default", "root", since=NOW - 3600, limit=3)
    assert result["nodes"] == []
    assert result["truncated"] is False


def test_unknown_recorded_ends_obey_window_and_limit(tmp_path):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root")
        for sid, ended in (("old", NOW - 7200), ("recent", NOW - 20), ("newest", NOW - 10), ("unended", None)):
            session(db, sid)
            launch(db, sid, cause="unknown", finished=ended)
    family = Family({"default": read_profile("default", path)}, now=NOW)
    result = family.session_family("default", "root", since=NOW - 3600, limit=2)
    assert {n["session_id"] for n in result["nodes"]} == {"newest", "unended"}
    assert {n["status"] for n in result["nodes"]} == {"unknown"}
    assert result["truncated"] is True


@pytest.mark.parametrize("requested", ["A", "B", "stale"])
def test_compressed_root_keeps_both_physical_launchers(tmp_path, requested):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "A", source="desktop", ended=NOW - 3600)
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id='A'")
        session(db, "B", source="desktop", parent="A")
        session(db, "stale", source="desktop", parent="A", ended=NOW - 100)
        for sid, parent in (("lane", "A"), ("helper", "B")):
            session(db, sid)
            launch(db, sid, launcher=parent, kind="lane" if sid == "lane" else "helper",
                   cause="error", finished=NOW)
    family = Family({"default": read_profile("default", path)}, now=NOW,
                    live={("default", "A"): {"running": False}})
    result = family.session_family("default", requested, since=0)
    assert result["root"]["session_id"] == "B"
    assert {n["session_id"] for n in result["nodes"]} == {"lane", "helper"}
    assert all(n["parent_node_id"] == result["root"]["node_id"] for n in result["nodes"])
    overview = family.overview(since=0)
    assert {n["session_id"] for n in overview["failed"]} == {"lane", "helper"}
    assert [n["root"]["session_id"] for n in overview["earlier"]] == ["B"]


@pytest.mark.parametrize("launcher_profile", ["denied", "default"])
def test_overview_does_not_route_to_an_unreadable_launcher(tmp_path, launcher_profile):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "child")
        launch(db, "child", kind="lane", launcher="private", launcher_profile=launcher_profile,
               cause="error", finished=NOW)
    failed = Family({"default": read_profile("default", path)}, now=NOW).overview(since=0)["failed"]
    assert len(failed) == 1
    node = failed[0]
    assert node["root_session_id"] is None
    assert node["root_profile"] is None


def test_compressed_teammate_keeps_recorded_kind_and_descendants(tmp_path):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root", source="desktop")
        session(db, "thread", source="desktop", ended=NOW - 3600)
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id='thread'")
        session(db, "tip", source="desktop", parent="thread")
        launch(db, "thread", kind="teammate")
        session(db, "helper", source="subagent", parent="tip")
        launch(db, "helper", launcher="tip")
    result = Family({"default": read_profile("default", path)}, now=NOW).session_family("default", "root", since=0)
    nodes = {n["session_id"]: n for n in result["nodes"]}
    assert set(nodes) == {"tip", "helper"}
    assert nodes["tip"]["kind"] == "teammate"
    assert nodes["helper"]["parent_node_id"] == nodes["tip"]["node_id"]
