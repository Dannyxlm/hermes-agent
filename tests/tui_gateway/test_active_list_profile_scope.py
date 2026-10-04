"""Display-only roster scope through the real dispatcher and fixture profile homes."""
import threading

import pytest

from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server


def records(home, peer):
    server._sessions.update({
        "launch": {"session_key": "same", "transport": peer},
        "ops": {"session_key": "same", "profile_home": str(home / "profiles" / "ops"),
                "transport": server._detached_ws_transport, "running": True},
        "final": {"session_key": "gone", "_finalized": True},
    })


def test_profile_filter_is_display_only_and_round_trips(mobile_home, peer, monkeypatch):
    records(mobile_home, peer)
    monkeypatch.setattr(server, "_resolve_model", lambda: "fixture")
    original = dict(server._sessions)
    for profile, expected in [("default", "launch"), ("ops", "ops"), ("default", "launch")]:
        rows = rpc("session.active_list", profile=profile)["result"]["sessions"]
        assert [r["id"] for r in rows] == [expected]
        assert rows[0]["profile"] == profile
    assert server._sessions == original
    assert server._sessions["ops"]["transport"] is server._detached_ws_transport
    assert peer.frames == []


def test_omitted_or_empty_scope_keeps_all_profiles(mobile_home, peer, monkeypatch):
    records(mobile_home, peer)
    monkeypatch.setattr(server, "_resolve_model", lambda: "fixture")
    for params in ({}, {"profile": ""}):
        rows = rpc("session.active_list", **params)["result"]["sessions"]
        assert [(r["id"], r["profile"]) for r in rows] == [("launch", "default"), ("ops", "ops")]


def test_named_launch_reports_name(mobile_home, peer, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", mobile_home / "profiles" / "ops")
    monkeypatch.setattr(server, "_resolve_model", lambda: "fixture")
    server._sessions["launch"] = {"session_key": "same", "transport": peer}
    assert rpc("session.active_list", profile="ops")["result"]["sessions"][0]["profile"] == "ops"


def test_unknown_profile_errors_without_creation(mobile_home, peer):
    records(mobile_home, peer)
    assert rpc("session.active_list", profile="missing")["error"]["code"] == 4064
    assert not (mobile_home / "profiles" / "missing").exists()


@pytest.mark.parametrize("running,building,pending,status", [
    (False, False, None, "idle"), (True, False, None, "working"),
    (True, True, None, "starting"), (True, True, "approval", "waiting"),
])
def test_status_precedence(mobile_home, peer, monkeypatch, running, building, pending, status):
    monkeypatch.setattr(server, "_resolve_model", lambda: "fixture")
    monkeypatch.setattr(server, "_session_pending_kind", lambda sid: pending)
    server._sessions["launch"] = {"session_key": "same", "transport": peer, "running": running,
                                  "agent_ready": threading.Event(), "agent_build_started": building}
    assert rpc("session.active_list", profile="default")["result"]["sessions"][0]["status"] == status
