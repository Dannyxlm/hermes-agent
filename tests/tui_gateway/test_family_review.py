"""Legacy Family reads must not activate profile hosting or watch empty stores."""
import pytest

from tests.tui_gateway.test_methods_mobile import rpc

pytest_plugins = ["tests.tui_gateway.test_methods_mobile", "tests.tui_gateway.test_family"]


def test_malformed_profile_uses_subagent_not_found_shape(family_home):
    family = rpc("session.family", profile="Bad Name", session_id="root")
    subagent = rpc("subagent.list", profile="Bad Name", session_id="root")
    assert family["error"]["code"] == subagent["error"]["code"] == 4001


@pytest.mark.parametrize("stage", ["ownership", "storage", "projection"])
def test_internal_value_error_is_not_hidden_as_not_found(family_home, monkeypatch, stage):
    from hermes_family import Family
    from tui_gateway import server

    def broken(*args, **kwargs):
        raise ValueError("broken Family projection")

    if stage == "projection":
        monkeypatch.setattr(Family, "session_family", broken)
    else:
        monkeypatch.setattr(server, "_family_root_owned" if stage == "ownership" else "_family_profiles", broken)
    with pytest.raises(ValueError, match="broken Family projection"):
        rpc("session.family", profile="default", session_id="root", since=0)


def test_family_reads_do_not_activate_profile_watchers(family_home, monkeypatch):
    from tui_gateway import server
    def forbidden(*args):
        raise AssertionError("read activated profile hosting")
    monkeypatch.setattr(server, "_profile_home", forbidden)
    assert "result" in rpc("family.overview", since=0)
    assert "result" in rpc("session.family", profile="default", session_id="root", since=0)


def test_missing_profile_database_is_empty(family_home):
    empty = family_home / "profiles" / "empty"
    empty.mkdir()
    (empty / "config.yaml").write_text("{}")
    result = rpc("family.overview", since=0)
    assert "result" in result, result
    assert result["result"]["complete"] is True
    assert result["result"]["unavailable_profiles"] == []


def test_unavailable_live_profile_is_reported(family_home, monkeypatch):
    from tui_gateway import server
    original = server._current_session_steer_authority
    def missing(sid):
        if sid == "ops":
            raise server.ProfileUnavailableError("ops")
        return original(sid)
    monkeypatch.setattr(server, "_current_session_steer_authority", missing)
    result = rpc("family.overview", since=0)
    assert "result" in result, result
    assert result["result"]["complete"] is False
    assert result["result"]["unavailable_profiles"] == ["ops"]


@pytest.mark.parametrize("requested", ["root", "tip"])
def test_legacy_compressed_root_keeps_family_and_transport_ownership(family_home, requested):
    from hermes_state import SessionDB
    from tui_gateway import server
    from tests.test_hermes_family import launch
    with SessionDB(db_path=family_home / "state.db") as db:
        db._conn.execute("UPDATE sessions SET ended_at=1,end_reason='compression' WHERE id='root'")
        db.create_session("tip", source="test", parent_session_id="root")
        db.create_session("late", source="subagent", parent_session_id="tip")
        launch(db._conn, "late", launcher="tip")
        db._conn.commit()
    server._sessions["default"].update(session_key="tip", agent_running=True)
    result = rpc("session.family", profile="default", session_id=requested, since=0)
    assert "result" in result, result
    assert result["result"]["root"]["session_id"] == "tip"
    assert result["result"]["root"]["status"] == "working"
    assert {n["session_id"] for n in result["result"]["nodes"]} == {"ops-root", "helper", "late"}
