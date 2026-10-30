"""The legacy Family transport reads the same systemd receipt without hosting the process."""
import pytest

from hermes_state import SessionDB
from tests.gateway.test_family_systemd import fake_systemctl  # noqa: F401
from tests.tui_gateway.test_methods_mobile import rpc
from tui_gateway import server

pytest_plugins = ["tests.tui_gateway.test_methods_mobile"]
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize("state,status", [("active", "working"), ("failed", "failed"), ("not-found", "unknown")])
def test_legacy_systemd_family(mobile_home, peer, monkeypatch, fake_systemctl, state, status):
    monkeypatch.setenv("UNIT_TEST_STATE", state)
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: mobile_home)
    with SessionDB(mobile_home / "state.db") as db:
        db.create_session("root", "cli")
        db.record_launch("unit", kind="process", launcher_profile="default", launcher_session_id="root",
                         child_profile="default", backend_ref="systemd:example")
    server._sessions["root"] = {"session_key": "root", "profile_home": str(mobile_home), "transport": peer,
                                "agent_running": False, "history": []}
    result = rpc("session.family", profile="default", session_id="root")
    assert "result" in result, result
    node, = result["result"]["nodes"]
    assert node["status"] == status
    if state == "active":
        assert node["command"] == "Unit description"
    if state == "failed":
        assert node["exit_code"] == 23
    assert "stop" not in node["actions"]
