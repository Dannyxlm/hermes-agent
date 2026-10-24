"""Legacy Family reads must not activate profile hosting or watch empty stores."""
import pytest

from tests.tui_gateway.test_family import family_home
from tests.tui_gateway.test_methods_mobile import rpc

pytest_plugins = ["tests.tui_gateway.test_methods_mobile"]


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
