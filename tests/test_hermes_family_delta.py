"""Family windows apply to launch completion, never stale transcript teardown."""
import pytest

from hermes_family import Family, read_profile
from hermes_family_storage import read_graph
from tests.test_hermes_family import NOW, fixture_db, launch, session


@pytest.mark.parametrize("signal,expected", [({"running": True}, "working"),
    ({"pending_kind": "approval", "can_answer": True}, "needs_you")])
@pytest.mark.parametrize("walk", ["root", "overview"])
def test_live_launch_survives_old_session_teardown(tmp_path, signal, expected, walk):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root")
        session(db, "lane", ended=NOW - 86400)
        launch(db, "lane", kind="lane")
    profiles, unavailable = read_graph({"default": path}, read_profile,
        root=("default", "root") if walk == "root" else None, since=NOW - 43200)
    family = Family(profiles, live={("default", "lane"): signal}, now=NOW, unavailable_profiles=unavailable)
    result = family.session_family("default", "root", since=NOW - 43200, limit=1)
    assert len(result["nodes"]) == 1
    node = result["nodes"][0]
    assert node["status"] == expected
    assert node["ended_at"] is None
    overview = family.overview(since=NOW - 43200)
    assert overview["team"][0]["state"] == ("needs_you" if expected == "needs_you" else "working")
    assert len(overview["roots"]) == 1
    assert len(overview["needs_you"]) == int(expected == "needs_you")


@pytest.mark.parametrize("kind,signal,expected", [
    ("lane", {"running": True}, "working"),
    ("lane", {"running": True, "last_activity_at": NOW - 1200}, "quiet"),
    ("helper", {"pending_kind": "approval", "can_answer": True}, "needs_you"),
    ("teammate", {"pending_kind": "clarify", "can_answer": True}, "needs_you"),
    ("process", {"running": True, "can_stop": True}, "working"),
])
@pytest.mark.parametrize("walk", ["root", "overview"])
def test_positive_live_evidence_supersedes_old_restart(tmp_path, kind, signal, expected, walk):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root")
        session(db, "resumed", ended=NOW - 172800,
                activity=signal.get("last_activity_at", NOW - 10))
        db.execute("UPDATE sessions SET end_reason='ws_orphan_reap' WHERE id='resumed'")
        launch(db, "resumed", kind=kind, cause="owner_restart", finished=NOW - 172800)
    profiles, unavailable = read_graph({"default": path}, read_profile,
        root=("default", "root") if walk == "root" else None, since=NOW - 43200)
    family = Family(profiles, live={("default", "resumed"): signal}, now=NOW,
                    unavailable_profiles=unavailable)
    node, = family.session_family("default", "root")["nodes"]
    assert node["status"] == expected
    assert "resume" not in node["actions"]
    if expected == "needs_you":
        assert "answer" in node["actions"]
        assert node["needs_you"]["kind"] == signal["pending_kind"]
    overview = family.overview()
    assert overview["team"] == [{"profile": "default", "state":
        "needs_you" if expected == "needs_you" else "working", "live_count": 1}]
    assert overview["roots"][0]["live_count"] == 1


@pytest.mark.parametrize("cause,expected", [("success", "done"), ("error", "failed"),
    ("user_stop", "stopped"), ("parent_stop", "stopped")])
def test_concrete_receipt_still_outranks_live_evidence(tmp_path, cause, expected):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root")
        session(db, "finished")
        launch(db, "finished", kind="lane", cause=cause, finished=NOW)
    family = Family({"default": read_profile("default", path)}, now=NOW,
        live={("default", "finished"): {"running": True, "pending_kind": "approval"}})
    node, = family.session_family("default", "root")["nodes"]
    assert node["status"] == expected
    assert node["actions"] == ["open"]


@pytest.mark.parametrize("cause,expected", [("success", "done"), ("error", "failed")])
def test_later_runtime_receipt_supersedes_restart(tmp_path, cause, expected):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root")
        session(db, "finished")
        launch(db, "finished", kind="lane", cause="owner_restart", finished=NOW - 172800)
    family = Family({"default": read_profile("default", path)}, now=NOW,
        live={("default", "finished"): {"terminal_cause": cause, "ended_at": NOW}})
    node, = family.session_family("default", "root", since=0)["nodes"]
    assert node["status"] == expected
    assert "resume" not in node["actions"]


@pytest.mark.parametrize("walk", ["root", "overview"])
def test_unresumed_restart_survives_window_and_limit(tmp_path, walk):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root")
        for sid in ("paused", "finished"):
            session(db, sid, ended=NOW - 86400)
        launch(db, "paused", kind="lane", cause="owner_restart", finished=NOW - 86400)
        launch(db, "finished", kind="lane", cause="success", finished=NOW)
    profiles, _ = read_graph({"default": path}, read_profile,
        root=("default", "root") if walk == "root" else None, since=NOW - 43200)
    result = Family(profiles, now=NOW).session_family("default", "root", limit=1)
    assert [n["session_id"] for n in result["nodes"]] == ["paused"]
    assert result["nodes"][0]["status"] == "paused_by_restart"
    assert "resume" in result["nodes"][0]["actions"]
