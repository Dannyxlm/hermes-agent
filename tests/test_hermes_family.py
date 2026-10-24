"""Family wire fixtures use recorded edges, never inferred titles or timestamps."""
import sqlite3

import pytest


LAUNCH_DDL = """
CREATE TABLE IF NOT EXISTS work_launches (
    launch_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    launcher_profile TEXT NOT NULL,
    launcher_session_id TEXT NOT NULL,
    parent_launch_id TEXT,
    child_profile TEXT NOT NULL,
    child_session_id TEXT,
    backend_ref TEXT,
    batch_id TEXT,
    batch_name TEXT,
    title TEXT,
    created_at REAL NOT NULL,
    bound_at REAL,
    finished_at REAL,
    terminal_cause TEXT,
    next_run_at REAL
);
CREATE INDEX IF NOT EXISTS idx_work_launches_launcher ON work_launches(launcher_profile, launcher_session_id);
CREATE INDEX IF NOT EXISTS idx_work_launches_child ON work_launches(child_session_id);
CREATE INDEX IF NOT EXISTS idx_work_launches_batch ON work_launches(batch_id);
"""
NOW = 200000.0


def fixture_db(path, *, old=False):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, "
                 "title TEXT, started_at REAL, ended_at REAL, end_reason TEXT, last_activity_at REAL, "
                 "last_activity_description TEXT, model TEXT)")
    if not old:
        conn.executescript(LAUNCH_DDL)
        conn.execute("ALTER TABLE sessions ADD COLUMN last_activity_kind TEXT")
        conn.execute("ALTER TABLE sessions ADD COLUMN last_activity_tool TEXT")
    return conn


def session(conn, sid, *, source="tool", parent=None, activity=NOW - 10, ended=None):
    conn.execute("INSERT INTO sessions (id,source,parent_session_id,title,started_at,ended_at,last_activity_at) "
                 "VALUES (?,?,?,?,?,?,?)", (sid, source, parent, f"Full {sid}", NOW - 5000, ended, activity))


def launch(conn, sid, *, profile="default", launcher="root", launcher_profile="default", kind="helper",
           batch=None, cause=None, finished=None, parent_launch=None, backend=None, next_run=None):
    conn.execute("INSERT INTO work_launches (launch_id,kind,launcher_profile,launcher_session_id,"
                 "parent_launch_id,child_profile,child_session_id,backend_ref,batch_id,batch_name,title,"
                 "created_at,finished_at,terminal_cause,next_run_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, kind, launcher_profile, launcher, parent_launch, profile,
                  None if kind in {"process", "check_in"} else sid, backend, batch,
                  "Fan out" if batch else None, sid, NOW - 4000, finished, cause, next_run))


@pytest.fixture
def night(tmp_path):
    from hermes_family import read_profile, Family
    paths = {p: tmp_path / f"{p}.db" for p in ("default", "iris")}
    live = {}
    with fixture_db(paths["default"]) as db, fixture_db(paths["iris"]) as other:
        session(db, "root", source="desktop")
        for batch, count in (("six", 6), ("five", 5)):
            for i in range(count):
                sid = f"{batch}-{i}"
                session(db, sid, source="subagent", parent="root")
                launch(db, sid, batch=batch, cause="success", finished=NOW - 100)
        for i in range(8):
            sid = f"lane-{i}"
            session(db, sid, activity=NOW - (1200 if i == 1 else 10))
            launch(db, sid, kind="lane", cause="owner_restart" if i == 0 else None)
            if i > 0:
                live[("default", sid)] = {"running": True}
        session(other, "thread")
        launch(other, "thread", profile="iris", kind="teammate")
        session(other, "thread-helper", source="subagent", parent="thread")
        launch(other, "thread-helper", profile="iris", launcher="thread", launcher_profile="iris")
        live[("iris", "thread")] = {"running": True}
        live[("iris", "thread-helper")] = {"running": True, "can_stop": True}
        launch(db, "check", kind="check_in", backend="job", next_run=NOW + 60)
        for i, cause in enumerate(("success", "error")):
            sid = f"run-{i}"
            session(db, sid)
            launch(db, sid, kind="check_in_run", parent_launch="check", cause=cause, finished=NOW - 50)
        for i, cause in enumerate((None, "user_stop")):
            sid = f"process-{i}"
            launch(db, sid, kind="process", backend=sid, cause=cause, finished=NOW - 30 if cause else None)
        live[("default", "process-0")] = {"running": True, "can_stop": True, "command": "fixture command"}
        session(db, "approval", source="subagent", parent="root")
        launch(db, "approval")
        live[("default", "approval")] = {"running": True, "pending_kind": "approval", "can_answer": True}
        session(db, "unlinked")
    snapshots = {p: read_profile(p, path) for p, path in paths.items()}
    return Family(snapshots, live=live, now=NOW), paths, live


def test_28_node_night(night):
    family, _, _ = night
    result = family.session_family("default", "root", since=0)
    assert len(result["nodes"]) + 1 == 28
    assert result["complete"] and not result["truncated"]
    nodes = {n["title"]: n for n in result["nodes"]}
    assert nodes["lane-0"]["status"] == "paused_by_restart"
    assert nodes["lane-0"]["actions"] == ["open", "resume"]
    assert nodes["lane-1"]["status"] == "quiet"
    assert nodes["thread-helper"]["parent_node_id"] == nodes["thread"]["node_id"]
    assert nodes["run-0"]["parent_node_id"] == nodes["check"]["node_id"]
    assert nodes["approval"]["status"] == "needs_you"
    assert nodes["approval"]["needs_you"] == {"kind": "approval", "session_id": "approval", "profile": "default"}
    assert nodes["approval"]["actions"] == ["open", "answer"]
    assert nodes["run-1"]["status"] == "failed"
    assert nodes["process-1"]["status"] == "stopped"
    assert nodes["six-0"]["status"] == "done"
    assert result["root"]["status"] == "unknown"
    assert sorted(b["size"] for b in result["batches"]) == [5, 6]
    assert "unlinked" not in {n["session_id"] for n in result["nodes"]}
    overview = family.overview(since=0)
    assert len(overview["roots"]) == 1
    assert overview["roots"][0]["total_count"] == 28
    assert overview["roots"][0]["needs_you_count"] == 1
    assert overview["coming_up"][0]["title"] == "check"
    assert overview["needs_you"][0]["title"] == "approval"
    assert {t["profile"]: t["state"] for t in overview["team"]} == {"default": "needs_you", "iris": "working"}


def test_limit_keeps_live_and_attention(night):
    family, _, _ = night
    result = family.session_family("default", "root", since=0, limit=1)
    assert result["truncated"]
    assert {n["title"] for n in result["nodes"]} >= {"approval", "lane-1", "thread", "thread-helper", "process-0"}
    assert not any(n["title"].startswith("six-") for n in result["nodes"])


def test_unavailable_profile_is_omitted(night):
    from hermes_family import Family
    family, _, live = night
    scoped = Family({"default": family.profiles["default"]}, live=live, now=NOW, unavailable_profiles=["iris"])
    result = scoped.session_family("default", "root", since=0)
    assert result["unavailable_profiles"] == ["iris"] and result["complete"] is False
    assert all(n["profile"] == "default" for n in result["nodes"])
    with pytest.raises(KeyError):
        scoped.session_family("iris", "thread")


def test_old_database_only_recorded_delegate_edges(tmp_path):
    from hermes_family import read_profile, Family
    path = tmp_path / "old.db"
    with fixture_db(path, old=True) as db:
        session(db, "root", source="desktop")
        session(db, "helper", source="subagent", parent="root")
        session(db, "branch", source="desktop", parent="root")
        session(db, "unlinked")
    family = Family({"default": read_profile("default", path)}, now=NOW)
    result = family.session_family("default", "root")
    assert result["complete"] is True
    assert [(n["session_id"], n["title"], n["batch_id"], n["status"]) for n in result["nodes"]] == [
        ("helper", None, None, "unknown")]


@pytest.mark.parametrize("kind,threshold", [("helper", 600), ("background", 600), ("process", 600),
                                           ("lane", 1200), ("teammate", 1200), ("check_in_run", 600)])
def test_quiet_requires_positive_liveness(kind, threshold):
    from hermes_family import status_for
    row = {"kind": kind, "last_activity_at": NOW - threshold, "ended_at": None}
    assert status_for(row, {}, NOW) == "unknown"
    assert status_for(row, {"running": True}, NOW) == "quiet"
    assert status_for({**row, "last_activity_at": NOW - threshold + 1}, {"running": True}, NOW) == "working"
    assert status_for({**row, "last_activity_at": None}, {"running": True}, NOW) == "working"


@pytest.mark.parametrize("cause,status", [("success", "done"), ("error", "failed"), ("failed_start", "failed"),
                                          ("user_stop", "stopped"), ("parent_stop", "stopped"),
                                          ("owner_restart", "paused_by_restart"), ("unknown", "needs_you")])
def test_terminal_precedence(cause, status):
    from hermes_family import status_for
    assert status_for({"kind": "helper", "terminal_cause": cause},
                      {"pending_kind": "approval", "running": True}, NOW) == status


def test_read_only_constant_query_count(night):
    from hermes_family import read_profile
    _, paths, _ = night
    counts = []
    for profile, path in paths.items():
        statements = []
        snapshot = read_profile(profile, path, trace=statements.append)
        assert snapshot.sessions
        counts.append(len(statements))
    assert max(counts) <= 8


def test_since_does_not_hide_live_descendants_or_their_ancestors(night):
    family, _, _ = night
    result = family.session_family("default", "root", since=NOW)
    assert "approval" in {n["title"] for n in result["nodes"]}
    assert "six-0" not in {n["title"] for n in result["nodes"]}


@pytest.mark.parametrize("reason", ["cli_close", "tui_close", "ws_disconnect", "idle_timeout", "agent_close",
                                    "startup_orphan_reap", "compression", "session_reset", "cron_complete"])
def test_end_reason_and_null_end_never_claim_completion_or_liveness(tmp_path, reason):
    from hermes_family import read_profile, Family
    path = tmp_path / "old.db"
    with fixture_db(path, old=True) as db:
        session(db, "root", source="desktop")
        session(db, "helper", source="subagent", parent="root", ended=NOW)
        db.execute("UPDATE sessions SET end_reason=? WHERE id='helper'", (reason,))
    result = Family({"default": read_profile("default", path)}, now=NOW).session_family("default", "root")
    assert result["nodes"][0]["status"] == "unknown"
    assert result["nodes"][0]["actions"] == ["open"]
    assert result["nodes"][0]["step_kind"] is None


def test_activity_is_typed_not_parsed_and_terminal_reply_is_only_a_hint(tmp_path):
    from hermes_family import read_profile, Family
    path = tmp_path / "activity.db"
    with fixture_db(path) as db:
        session(db, "root", source="desktop")
        for sid, cause in (("lane", "success"), ("helper", None)):
            session(db, sid)
            launch(db, sid, kind="lane" if sid == "lane" else "helper", cause=cause, finished=NOW if cause else None)
        db.execute("UPDATE sessions SET last_activity_description='ERROR executing tool: fake' WHERE id='helper'")
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT)")
        db.execute("INSERT INTO messages VALUES (1,'lane','assistant','Verified fixture result\nDetails',NULL)")
        db.execute("INSERT INTO messages VALUES (2,'lane','assistant','Not a final reply','[{}]')")
    snapshot = read_profile("default", path)
    family = Family({"default": snapshot}, live={("default", "helper"): {"running": True}}, now=NOW)
    nodes = {n["session_id"]: n for n in family.session_family("default", "root", since=0)["nodes"]}
    assert nodes["lane"]["status"] == "done" and nodes["lane"]["report_hint"] == "Verified fixture result"
    assert nodes["helper"]["status"] == "working" and nodes["helper"]["step_kind"] is None
    snapshot.sessions["helper"]["last_activity_kind"] = "tool"
    snapshot.sessions["helper"]["last_activity_tool"] = "actual_tool"
    nodes = Family({"default": snapshot}, live={("default", "helper"): {"running": True}}, now=NOW).session_family("default", "root")["nodes"]
    helper = next(n for n in nodes if n["session_id"] == "helper")
    assert (helper["step_kind"], helper["step_tool"]) == ("tool", "actual_tool")


def test_bfs_old_parent_still_connects_live_grandchild_and_depth_is_bounded(tmp_path):
    from hermes_family import read_profile, Family
    path = tmp_path / "deep.db"
    with fixture_db(path) as db:
        session(db, "root", source="desktop")
        parent = "root"
        for i in range(70):
            sid = f"child-{i}"
            session(db, sid, source="subagent", parent=parent)
            launch(db, sid, launcher=parent, cause="success" if i == 0 else None, finished=1 if i == 0 else None)
            parent = sid
    family = Family({"default": read_profile("default", path)}, live={("default", "child-1"): {"running": True}}, now=NOW)
    result = family.session_family("default", "root", since=NOW - 1, limit=1)
    assert result["truncated"] is True
    nodes = {n["session_id"]: n for n in result["nodes"]}
    assert len(nodes) == 64
    assert nodes["child-1"]["parent_node_id"] == nodes["child-0"]["node_id"]
    assert nodes["child-0"]["status"] == "done"


def test_failed_start_without_child_and_deleted_child_no_resurrection(tmp_path):
    from hermes_family import read_profile, Family
    path = tmp_path / "failed.db"
    with fixture_db(path) as db:
        session(db, "root", source="desktop")
        launch(db, "not-started", kind="lane", cause="failed_start", finished=NOW)
        db.execute("UPDATE work_launches SET child_session_id=NULL WHERE launch_id='not-started'")
        launch(db, "deleted-child")
    family = Family({"default": read_profile("default", path)}, now=NOW)
    result = family.session_family("default", "root", since=0)
    assert len(result["nodes"]) == 1
    assert (result["nodes"][0]["title"], result["nodes"][0]["status"]) == ("not-started", "failed")
    earlier = family.overview(since=0)["earlier"]
    assert earlier[0]["total_count"] == 2 and earlier[0]["failed_count"] == 1


def test_limit_removes_oldest_finished_before_newer_finished(tmp_path):
    from hermes_family import read_profile, Family
    path = tmp_path / "limited.db"
    with fixture_db(path) as db:
        session(db, "root", source="desktop")
        for sid, finished in (("old", NOW - 100), ("new", NOW)):
            session(db, sid)
            launch(db, sid, cause="success", finished=finished)
    family = Family({"default": read_profile("default", path)}, now=NOW)
    result = family.session_family("default", "root", since=0, limit=1)
    assert result["truncated"] is True
    assert [n["session_id"] for n in result["nodes"]] == ["new"]



def test_unknown_profile_provenance_is_not_an_open_permission(tmp_path):
    from hermes_family import read_profile, Family
    path = tmp_path / "profiles.db"
    with fixture_db(path) as db:
        session(db, "root", source="desktop")
        session(db, "invalid-location")
        launch(db, "invalid-location", profile="unknown")
    result = Family({"default": read_profile("default", path)}, now=NOW).session_family("default", "root")
    assert result["nodes"] == []
    assert result["complete"] is False and result["unavailable_profiles"] == ["unknown"]


def test_query_bound_with_real_schema_and_hundreds_of_launches(tmp_path):
    from hermes_state import SessionDB
    from hermes_family import read_profile, Family
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db._conn.executescript(LAUNCH_DDL)
        db.create_session("root", source="test")
        for i in range(300):
            db.create_session(f"helper-{i}", source="subagent", parent_session_id="root")
            launch(db._conn, f"helper-{i}")
        db._conn.commit()
    statements = []
    snapshot = read_profile("default", path, trace=statements.append)
    assert len(statements) <= 9
    result = Family({"default": snapshot}, now=NOW).session_family("default", "root", limit=500)
    assert len(result["nodes"]) == 300 and not result["truncated"]
    assert all(n["status"] == "unknown" for n in result["nodes"])



def test_failed_inbox_waits_for_helper_parent_turn_but_not_other_work(tmp_path):
    from hermes_family import read_profile, Family
    path = tmp_path / "failed-inbox.db"
    with fixture_db(path) as db:
        session(db, "root", source="desktop")
        for i, kind in enumerate(("helper", "lane", "teammate", "check_in_run", "process", "background")):
            if kind != "process":
                session(db, kind)
            launch(db, kind, kind=kind, cause="error", finished=NOW - 10 + i)
    profiles = {"default": read_profile("default", path)}
    running = Family(profiles, live={("default", "root"): {"running": True}}, now=NOW).overview(since=0)
    assert [n["kind"] for n in running["failed"]] == ["background", "process", "check_in_run", "teammate", "lane"]
    idle = Family(profiles, live={("default", "root"): {"running": False}}, now=NOW).overview(since=0)
    assert [n["kind"] for n in idle["failed"]] == ["background", "process", "check_in_run", "teammate", "lane", "helper"]
    unknown = Family(profiles, now=NOW).overview(since=0)
    assert "helper" not in {n["kind"] for n in unknown["failed"]}
    assert Family(profiles, now=NOW).overview(since=NOW)["failed"] == []
