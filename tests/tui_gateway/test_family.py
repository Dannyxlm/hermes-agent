"""Legacy Family dispatcher parity, capability and transport ownership."""
import pytest

from hermes_state import SessionDB
from tests.test_hermes_family import LAUNCH_DDL, launch
from tests.tui_gateway.test_methods_mobile import rpc

pytest_plugins = ["tests.tui_gateway.test_methods_mobile"]
from tui_gateway import server


@pytest.fixture
def family_home(mobile_home, peer, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: mobile_home)
    for profile, sid, home in (("default", "root", mobile_home), ("ops", "ops-root", mobile_home / "profiles" / "ops")):
        with SessionDB(db_path=home / "state.db") as db:
            db._conn.executescript(LAUNCH_DDL)
            if profile == "ops":
                launch(db._conn, "ops-root", profile="ops", kind="teammate")
                db.create_session("helper", source="subagent", parent_session_id="ops-root")
                launch(db._conn, "helper", profile="ops", launcher="ops-root", launcher_profile="ops")
            db._conn.commit()
        server._sessions[profile] = {"session_key": sid, "profile_home": str(home), "transport": peer,
                                    "agent_running": profile == "ops", "history": []}
    return mobile_home


def test_legacy_family_uses_stored_id_and_cross_profile_db(family_home):
    result = rpc("session.family", profile="default", session_id="root", since=0)
    assert "result" in result, result
    family = result["result"]
    assert family["complete"] is True
    assert {n["session_id"] for n in family["nodes"]} == {"ops-root", "helper"}
    nodes = {n["session_id"]: n for n in family["nodes"]}
    assert nodes["ops-root"]["status"] == "working"
    assert nodes["helper"]["status"] == "unknown"
    assert rpc("mobile.capabilities")["result"]["family"] == {"protocol_version": 1}
    overview = rpc("family.overview", since=0)["result"]
    assert overview["roots"][0]["root"]["session_id"] == "root"
    assert len(server._sessions) == 2


def test_legacy_not_found_matches_subagents(family_home, peer):
    reference = rpc("subagent.list", session_id="missing")["error"]
    for profile, sid in (("default", "missing"), ("missing", "root"), ("ops", "root")):
        result = rpc("session.family", profile=profile, session_id=sid)
        assert result["error"] == reference
    server._sessions["default"]["transport"] = object()
    assert rpc("session.family", profile="default", session_id="root")["error"] == reference


def test_legacy_profile_bound_read_omits_foreign(family_home):
    from tui_gateway.ws_legacy_fallback import _connection_profile_home
    token = _connection_profile_home.set(str(family_home / "profiles" / "ops"))
    try:
        result = rpc("session.family", profile="ops", session_id="ops-root", since=0)["result"]
        assert result["complete"] is False and result["unavailable_profiles"] == ["default"]
        assert {n["profile"] for n in result["nodes"]} == {"ops"}
        overview = rpc("family.overview")["result"]
        assert {t["profile"] for t in overview["team"]} == {"ops"}
        assert "error" in rpc("session.family", profile="default", session_id="root")
    finally:
        _connection_profile_home.reset(token)


@pytest.mark.parametrize("params", [{}, {"profile": "default"}, {"profile": "default", "session_id": "root", "limit": 501},
    {"profile": "default", "session_id": "root", "limit": True},
    {"profile": "default", "session_id": "root", "since": "bad"}])
def test_invalid_family_params_are_refused(family_home, params):
    assert "error" in rpc("session.family", **params)
