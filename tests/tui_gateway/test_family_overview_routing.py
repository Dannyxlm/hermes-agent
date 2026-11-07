"""Legacy Overview accepts a profile envelope without widening ticket grants."""
import pytest

from tests.tui_gateway.test_methods_mobile import rpc

pytest_plugins = ["tests.tui_gateway.test_methods_mobile", "tests.tui_gateway.test_family"]


@pytest.mark.parametrize("profile", ["default", "ops"])
def test_host_overview_accepts_shared_profile_routing(family_home, profile):
    result = rpc("family.overview", profile=profile, since=0)
    assert "result" in result, result
    assert {row["profile"] for row in result["result"]["team"]} == {"default", "ops"}


def test_profile_ticket_cannot_use_overview_envelope_to_read_sibling(family_home):
    from tui_gateway.ws_legacy_fallback import _connection_profile_home
    token = _connection_profile_home.set(str(family_home / "profiles" / "ops"))
    try:
        allowed = rpc("family.overview", profile="ops", since=0)
        assert {row["profile"] for row in allowed["result"]["team"]} == {"ops"}
        assert "error" in rpc("family.overview", profile="default")
    finally:
        _connection_profile_home.reset(token)


def test_profile_ticket_boundary_binds_and_restores_legacy_overview_scope(family_home, peer, monkeypatch):
    import threading
    from types import SimpleNamespace
    from tui_gateway import server
    from tui_gateway.ws_legacy_fallback import connection_profile_home, dispatch_legacy

    actor = SimpleNamespace(profile_id=str(family_home / "profiles" / "ops"))
    replied = threading.Event()
    write = peer.write

    def capture(frame):
        result = write(frame)
        if frame.get("id") == "family-boundary":
            replied.set()
        return result

    monkeypatch.setattr(peer, "write", capture)
    for profile, readable in (("ops", True), ("default", False)):
        replied.clear()
        peer.frames.clear()
        response = dispatch_legacy(server, {"jsonrpc": "2.0", "id": "family-boundary", "method": "family.overview",
            "params": {"profile": profile, "since": 0}}, peer, actor)
        if response is None:
            assert replied.wait(10), "legacy dispatcher did not answer the scoped Overview"
            response = next(frame for frame in peer.frames if frame.get("id") == "family-boundary")
        assert ("result" in response) is readable, response
        if readable:
            assert {row["profile"] for row in response["result"]["team"]} == {"ops"}
        assert connection_profile_home() is None


@pytest.mark.parametrize("params", [{"profile": "default", "typo": True}, {"profile": 7}, {"profile": "missing"}])
def test_overview_keeps_strict_params_and_profile_validation(family_home, params):
    assert "error" in rpc("family.overview", **params)
