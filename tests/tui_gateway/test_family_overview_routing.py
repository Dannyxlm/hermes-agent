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


@pytest.mark.parametrize("params", [{"profile": "default", "typo": True}, {"profile": 7}, {"profile": "missing"}])
def test_overview_keeps_strict_params_and_profile_validation(family_home, params):
    assert "error" in rpc("family.overview", **params)
