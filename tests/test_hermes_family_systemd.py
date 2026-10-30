"""Off-turn unit polling must not resolve arbitrary skill credentials on a multiplexed host."""
import pytest

from hermes_family import Profile
from tests.gateway import test_family_systemd as systemd_fixtures

fake_systemctl = systemd_fixtures.fake_systemctl
pytestmark = pytest.mark.platforms("linux")


def test_systemd_poll_does_not_need_a_turn_secret_scope(monkeypatch, fake_systemctl):
    from agent.secret_scope import current_secret_scope, set_multiplex_context, reset_multiplex_context
    from hermes_family_systemd import systemd_signals
    from tools import env_passthrough
    monkeypatch.setattr(env_passthrough, "_allowed_env_vars", set())
    env_passthrough.register_env_passthrough(["UNRELATED_TOOL_KEY"])
    monkeypatch.setenv("UNRELATED_TOOL_KEY", "synthetic-only")
    monkeypatch.setenv("UNIT_TEST_STATE", "active")
    assert current_secret_scope() is None
    token = set_multiplex_context(True)
    try:
        signals = systemd_signals({"default": Profile("default", {}, [
            {"kind": "process", "backend_ref": "systemd:scope-probe", "finished_at": None}])})
        assert signals[("default", "systemd:scope-probe")]["running"] is True
    finally:
        reset_multiplex_context(token)
