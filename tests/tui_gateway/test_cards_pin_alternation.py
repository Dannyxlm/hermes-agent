"""A chat the app drew cards in keeps ``show_card`` when its turns alternate with ones the app isn't on.

Danny's lead chats get wake and thread-update turns between his phone turns. Those turns build an agent
with no card-drawing client, and used to re-pin the chat's tool array without ``show_card``. The next
phone turn then lost the tool when the process that ran it still remembered writing the earlier pin
(the tools[] freeze holds back anything the pin lacks), and even when it didn't, the array flipped
28 → 27 → 28 tools, a prompt-cache rewrite at every flip. A non-app turn now carries a ``show_card``
the chat's pin already has, while ``cards.enabled`` is on: one array across the alternation.

Each turn is ``_make_agent`` on the shared session row followed by ``_restore_pinned_tools``, what
``run_conversation`` does before a turn's first request. "Another process" is a separate pin-snapshot
table (``_PINNED_SNAPSHOTS`` is per process).
"""
from __future__ import annotations

import collections
import json
import threading

import pytest

from agent.conversation_loop import _restore_pinned_tools
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tools import mcp_tool_agent
from tools.registry import invalidate_check_fn_cache
from tui_gateway import cards_capability, server

KINDS = ["table", "flight", "route", "place", "weather", "chart", "document", "link", "gallery", "compose"]
KEY = "lead-chat"
PHONE_SID, WAKE_SID = "phone-sid", "wake-sid"


class Peer:
    def write(self, _frame):
        return True


def _flag(on: bool):
    (get_hermes_home() / "config.yaml").write_text(f"cards:\n  enabled: {str(on).lower()}\n", encoding="utf-8")
    invalidate_check_fn_cache()


def _session(transport):
    return {"session_key": KEY, "source": "tui", "transport": transport, "history": [],
            "history_lock": threading.Lock(), "history_version": 0}


@pytest.fixture
def chat(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(KEY, source="tui")
    runtime = {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "api_key": "test-key",
               "api_mode": "chat_completions"}
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda *_a: ("test-model", dict(runtime)))
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: ["file"])
    phone = Peer()
    cards_capability.advertise(phone, {"v": 1, "kinds": KINDS})
    monkeypatch.setitem(server._sessions, PHONE_SID, _session(phone))
    monkeypatch.setitem(server._sessions, WAKE_SID, _session(Peer()))  # nobody here draws cards
    with mcp_tool_agent._agent_tools_lock:
        mcp_tool_agent._PINNED_SNAPSHOTS.clear()
    _flag(True)
    yield db
    with mcp_tool_agent._agent_tools_lock:
        mcp_tool_agent._PINNED_SNAPSHOTS.clear()
    db.close()
    invalidate_check_fn_cache()


def _turn(db, sid):
    agent = server._make_agent(sid, KEY, session_id=KEY, session_db=db, platform_override="tui")
    _restore_pinned_tools(agent, db.get_session(KEY))
    return agent


def _in_other_process(monkeypatch, fn):
    """Run *fn* against its own pin-snapshot table, as a turn in another process would."""
    mine = mcp_tool_agent._PINNED_SNAPSHOTS
    monkeypatch.setattr(mcp_tool_agent, "_PINNED_SNAPSHOTS", collections.OrderedDict())
    try:
        return fn()
    finally:
        monkeypatch.setattr(mcp_tool_agent, "_PINNED_SNAPSHOTS", mine)


def _wire(agent) -> str:
    return json.dumps(agent.tools, sort_keys=False, separators=(",", ":"))


def _names(agent) -> list[str]:
    return [t["function"]["name"] for t in agent.tools]


@pytest.mark.parametrize("wake_elsewhere", [True, False], ids=["wake-in-another-process", "wake-in-this-process"])
def test_phone_wake_phone_keeps_show_card_and_one_tool_array(chat, monkeypatch, wake_elsewhere):
    first = _turn(chat, PHONE_SID)
    wake = (_in_other_process(monkeypatch, lambda: _turn(chat, WAKE_SID)) if wake_elsewhere
            else _turn(chat, WAKE_SID))
    second = _turn(chat, PHONE_SID)

    assert "show_card" in _names(first) and "show_card" in _names(second)
    assert "show_card" in _names(wake)  # carried from the pin; the card's alt text serves a client that can't draw
    assert _wire(first) == _wire(wake) == _wire(second)  # tools[] heads every request: no cache rewrite
    assert "show_card" in [t["function"]["name"] for t in json.loads(chat.get_session(KEY)["tool_names"])["tools"]]


def test_a_chat_no_app_ever_drew_in_never_gets_show_card(chat):
    for _ in range(3):
        assert "show_card" not in _names(_turn(chat, WAKE_SID))


def test_flag_off_a_wake_turn_drops_the_pinned_show_card(chat, monkeypatch):
    _turn(chat, PHONE_SID)
    _flag(False)
    wake = _in_other_process(monkeypatch, lambda: _turn(chat, WAKE_SID))
    assert "show_card" not in _names(wake)
    assert "show_card" not in wake.valid_tool_names


def test_a_profile_that_disables_cards_drops_the_pinned_show_card(chat, monkeypatch):
    _turn(chat, PHONE_SID)

    def wake_with_cards_disabled():
        agent = server._make_agent(WAKE_SID, KEY, session_id=KEY, session_db=chat, platform_override="tui")
        agent.disabled_toolsets = [*(agent.disabled_toolsets or []), "cards"]
        _restore_pinned_tools(agent, chat.get_session(KEY))
        return agent

    assert "show_card" not in _names(_in_other_process(monkeypatch, wake_with_cards_disabled))
