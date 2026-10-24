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
