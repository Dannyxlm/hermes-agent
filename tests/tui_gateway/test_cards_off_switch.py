"""``cards.enabled`` is the off switch, including for a chat that already showed a card.

A chat's tool pin (the session row's saved tool array) carries a pinned tool forward whenever the
agent's toolset selection names its toolset, before the tool's ``check_fn`` is asked. So the flag
has to keep ``cards`` out of the selection, and the handler refuses a call that still arrives.
Each restart below is a fresh ``_make_agent`` on the same session row after the process-local pin
snapshots are cleared, followed by ``_restore_pinned_tools``, which is what ``run_conversation``
does before the turn's first request.
"""
from __future__ import annotations

import json
import threading

import pytest

import model_tools
from agent.conversation_loop import _refresh_bot_chat_tools, _restore_pinned_tools
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tools import mcp_tool_agent
from tools.registry import invalidate_check_fn_cache
from tui_gateway import cards_capability, server

KINDS = ["table", "flight", "route", "place", "weather", "chart", "document", "link", "gallery", "compose"]
SID, KEY = "home-sid", "home"
PLACE = {"kind": "place", "title": "Café Olimpico", "data": {"name": "Café Olimpico", "lat": 45.5228, "lon": -73.6003}}


class Peer:
    def write(self, _frame):
        return True


def _flag(on: bool):
    (get_hermes_home() / "config.yaml").write_text(f"cards:\n  enabled: {str(on).lower()}\n", encoding="utf-8")
    invalidate_check_fn_cache()


@pytest.fixture
def home(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(KEY, source="tui")
    runtime = {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "api_key": "test-key",
               "api_mode": "chat_completions"}
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda *_a: ("test-model", dict(runtime)))
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: ["file"])
    phone = Peer()
    cards_capability.advertise(phone, {"v": 1, "kinds": KINDS})  # the phone stays attached throughout
    monkeypatch.setitem(server._sessions, SID, {"session_key": KEY, "source": "tui", "transport": phone, "history": [],
                                                 "history_lock": threading.Lock(), "history_version": 0})
    yield db
    with mcp_tool_agent._agent_tools_lock:
        mcp_tool_agent._PINNED_SNAPSHOTS.clear()
    db.close()
    invalidate_check_fn_cache()


def _restart_and_first_turn(db):
    with mcp_tool_agent._agent_tools_lock:
        mcp_tool_agent._PINNED_SNAPSHOTS.clear()
    agent = server._make_agent(SID, KEY, session_id=KEY, session_db=db, platform_override="tui")
    _restore_pinned_tools(agent, db.get_session(KEY))
    return agent


def _call(n, agent=None):
    return json.loads(model_tools.handle_function_call(
        "show_card", dict(PLACE), "task", tool_call_id=f"call-{n}", session_id=KEY, turn_id=f"turn-{n}",
        **({"enabled_tools": list(agent.valid_tool_names)} if agent is not None else {})))


def _offers(agent) -> bool:
    return "show_card" in agent.valid_tool_names or any(t["function"]["name"] == "show_card" for t in agent.tools)


def test_on_card_off_restart_on_again(home):
    _flag(True)
    first = _restart_and_first_turn(home)
    assert _offers(first) and _call(1, first)["ok"] is True  # a card shown
    assert home.get_session(KEY)["tool_names"]  # the chat's tool array is pinned with show_card in it

    _flag(False)
    off = _restart_and_first_turn(home)
    assert not _offers(off)
    assert _call(2, off)["ok"] is False
    refused = _call(3)  # even a call that reaches the handler directly
    assert refused["ok"] is False and "cards are off" in refused["error"]

    _flag(True)
    back = _restart_and_first_turn(home)
    assert _offers(back) and _call(4, back)["ok"] is True


def test_a_bot_chat_capability_refresh_keeps_the_tool_while_on_and_drops_it_when_off(home):
    """Review note 7: ``_refresh_bot_chat_tools`` re-derived the selection from config alone."""
    _flag(True)
    agent = server._make_agent(SID, KEY, session_id=KEY, session_db=home, platform_override="desktop")
    assert _offers(agent)
    _refresh_bot_chat_tools(agent)
    assert _offers(agent) and "cards" in agent.enabled_toolsets
    _flag(False)
    _refresh_bot_chat_tools(agent)
    assert not _offers(agent)
