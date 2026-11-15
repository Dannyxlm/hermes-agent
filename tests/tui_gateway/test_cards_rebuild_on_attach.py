"""Home gets Paper cards without a restart: when a card-drawing client attaches to a chat whose agent
was built without ``show_card``, the next turn's admission rebuilds that agent once.

Each test builds the agent through the real ``_make_agent`` (a real ``AIAgent``), advertises through
the real ``client.capabilities`` RPC, and admits turns through ``_prepare_turn_input`` (the
production turn entry), stopping right after the turn-start syncs.
"""
from __future__ import annotations

import threading

import pytest

from hermes_constants import get_hermes_home
from tools import approval_context
from tools.registry import invalidate_check_fn_cache
from tui_gateway import server

KINDS = ["table", "flight", "route", "place", "weather", "chart", "document", "link", "gallery", "compose"]
SID = "home-sid"


class _Admitted(Exception):
    pass


class Peer:
    def write(self, _frame):
        return True


def _config(enabled: bool):
    (get_hermes_home() / "config.yaml").write_text(f"cards:\n  enabled: {str(enabled).lower()}\n", encoding="utf-8")
    invalidate_check_fn_cache()


@pytest.fixture
def home(monkeypatch):
    """A Home-like session record whose agent was built while only a non-drawing client was attached."""
    _config(True)
    runtime = {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "api_key": "test-key",
               "api_mode": "chat_completions"}
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda *_a: ("test-model", dict(runtime)))
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: ["file"])
    builds = []
    real_make = server._make_agent

    def counted(*args, **kwargs):
        builds.append(kwargs.get("platform_override"))
        return real_make(*args, **kwargs)

    monkeypatch.setattr(server, "_make_agent", counted)
    peer = Peer()
    session = {"session_key": "home-key", "source": "desktop", "transport": peer, "history": [{"role": "user", "content": "hi"}],
               "history_lock": threading.Lock(), "history_version": 3}
    monkeypatch.setitem(server._sessions, SID, session)
    session["agent"] = server._make_agent(SID, "home-key", session_id="home-key", platform_override="desktop")
    session["agent"]._session_title_hint = "Bot Chat"
    builds.clear()
    yield session, peer, builds
    invalidate_check_fn_cache()


def _advertise(monkeypatch, peer, cards):
    monkeypatch.setattr(server, "_caller_transport", lambda: peer, raising=False)
    reply = server.handle_request({"id": "caps", "method": "client.capabilities", "params": {"cards": cards}})
    assert "error" not in reply, reply


def _admit_turn(monkeypatch, session, one_turn_restore=None):
    """Turn admission up to (and including) every turn-start sync; stop before history is snapshotted."""
    monkeypatch.setattr(server, "_profile_runtime_scope_tokens", lambda profile_home, **_kw: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    for name in ("_apply_pending_model_switch", "_sync_agent_model_with_config", "_sync_agent_compression_with_config",
                 "_sync_agent_fallback_with_config", "_sync_bot_capabilities"):
        monkeypatch.setattr(server, name, lambda sid, session: None)

    def stop(_session):
        raise _Admitted()

    monkeypatch.setattr(server, "_adopt_out_of_band_turns", stop)
    st = server._TurnRun(agent=None, one_turn_restore=one_turn_restore, terminal_callback=None, receipt_committed=False)
    try:
        with pytest.raises(_Admitted):
            server._prepare_turn_input(SID, session, st, "hello", [])
    finally:
        server._release_turn_scopes(SID, session, st)
    assert approval_context.get_current_session_key(default="") == ""
    return session["agent"]


def _has_cards(agent) -> bool:
    return "show_card" in agent.valid_tool_names


def test_attach_after_build_gives_the_next_turn_the_tool(monkeypatch, home):
    session, peer, builds = home
    before = session["agent"]
    assert not _has_cards(before)
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})
    assert session["agent"] is before  # attaching rebuilds nothing by itself

    after = _admit_turn(monkeypatch, session)
    assert after is not before and _has_cards(after) and "read_file" in after.valid_tool_names
    assert builds == ["desktop"]
    assert after._session_title_hint == "Bot Chat"
    assert session["history"] == [{"role": "user", "content": "hi"}] and session["history_version"] == 3


def test_attach_mid_turn_waits_for_the_turn_to_end(monkeypatch, home):
    session, peer, builds = home
    running = _admit_turn(monkeypatch, session)  # turn 1 admitted with no drawing client: it runs as built
    assert running is session["agent"] and not builds
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})  # the phone attaches while turn 1 runs
    assert session["agent"] is running and not _has_cards(running)  # turn 1's tool array never moves
    nxt = _admit_turn(monkeypatch, session)
    assert nxt is not running and _has_cards(nxt) and len(builds) == 1


def test_flag_off_rebuilds_nothing(monkeypatch, home):
    session, peer, builds = home
    _config(False)
    before = session["agent"]
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})
    assert _admit_turn(monkeypatch, session) is before and not builds


def test_a_second_attach_does_not_rebuild_again(monkeypatch, home):
    session, peer, builds = home
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})
    rebuilt = _admit_turn(monkeypatch, session)
    _advertise(monkeypatch, peer, None)
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})
    second = Peer()
    _advertise(monkeypatch, second, {"v": 1, "kinds": KINDS})
    assert _admit_turn(monkeypatch, session) is rebuilt and _admit_turn(monkeypatch, session) is rebuilt
    assert len(builds) == 1


def test_a_rebuild_that_still_lacks_the_tool_is_not_retried_every_turn(monkeypatch, home):
    session, peer, builds = home
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: [])  # the profile now offers no tools
    first = _admit_turn(monkeypatch, session)
    assert not _has_cards(first) and len(builds) == 1
    assert _admit_turn(monkeypatch, session) is first and len(builds) == 1


def test_no_drawing_client_no_rebuild(monkeypatch, home):
    session, peer, builds = home
    _advertise(monkeypatch, peer, {"v": 2, "kinds": ["hologram"]})
    before = session["agent"]
    assert _admit_turn(monkeypatch, session) is before and not builds


def test_a_failed_rebuild_keeps_the_turn_and_is_not_retried_every_turn(monkeypatch, home):
    session, peer, builds = home
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})
    before = session["agent"]

    def broken(*_args, **_kwargs):
        builds.append("broken")
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(server, "_make_agent", broken)
    assert _admit_turn(monkeypatch, session) is before  # this turn runs on the agent it had
    assert _admit_turn(monkeypatch, session) is before and builds == ["broken"]


def test_the_rebuild_keeps_the_chats_live_settings(monkeypatch, home):
    """Review finding 2: a resumed chat's stored effort and tier ride ``resume_runtime_overrides``, not
    the session pins, and the memory-review callback is wired after a build, not by it."""
    session, peer, builds = home
    stored = {"reasoning_config_override": {"enabled": True, "effort": "high"}, "service_tier_override": "priority"}
    session["resume_runtime_overrides"] = stored
    before = server._make_agent(SID, "home-key", session_id="home-key", platform_override="desktop", **stored)
    before._session_title_hint = "Bot Chat"
    session["agent"] = before
    server._wire_session_agent(SID, "home-key", before)
    builds.clear()

    def live(agent):
        return {"review_cb": agent.background_review_callback is not None, "notifications": agent.memory_notifications,
                "reasoning": agent.reasoning_config, "tier": agent.service_tier, "model": agent.model}

    seen = live(before)
    assert seen["review_cb"] and seen["reasoning"] == {"enabled": True, "effort": "high"} and seen["tier"] == "priority"
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})
    after = _admit_turn(monkeypatch, session)
    assert after is not before and _has_cards(after) and len(builds) == 1
    assert live(after) == seen


def test_a_model_once_turn_keeps_its_agent_and_cards_come_the_turn_after(monkeypatch, home):
    """Review note 10: a ``/model --once`` turn runs on its one-turn agent; rebuilding it would drop the
    once-model. The card rebuild waits for the next ordinary turn."""
    session, peer, builds = home
    _advertise(monkeypatch, peer, {"v": 1, "kinds": KINDS})
    once = session["agent"]
    assert _admit_turn(monkeypatch, session, one_turn_restore={"model": "the-usual"}) is once and not builds
    assert _has_cards(_admit_turn(monkeypatch, session)) and len(builds) == 1
