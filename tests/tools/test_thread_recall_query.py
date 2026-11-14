"""A thread's memory recall searches with its brief, not with the thread tool's framing.

The header names the launching chat ("↳ From Ava in “<title>” · thread tool"): its title pulled the same
release pages into every thread opened from one chat, and the launcher's name alias-matched a people page.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.turn_context import _memory_turn_start_and_prefetch
from tools.thread_state import recall_query_text
from tools.thread_tool import Caller, _brief_footer, _header


def _caller(title: str = "Verify merges, releases, and live services") -> Caller:
    return Caller(session_id="parent-1", lineage=frozenset({"parent-1"}), profile="default", home=Path("/nonexistent"),
                  subject="owner", title=title, cwd=None, name="Ava", toolsets=None, launch=None)


BRIEF = "Lane S: find why the first Codex call of a thread gets HTTP 400.\n\nDetails follow."


def test_brief_and_follow_up_lose_their_framing():
    caller = _caller()
    brief_message = f"{_header(caller)}\n\n{BRIEF}{_brief_footer(caller)}"  # thread_tool create
    follow_up = f"{_header(caller)}\n\nAlso check the cron runs."  # thread_tool send
    assert recall_query_text(brief_message) == BRIEF
    assert recall_query_text(follow_up) == "Also check the cron runs."


def test_untitled_launcher_header_is_stripped_too():
    caller = _caller(title="")
    assert recall_query_text(f"{_header(caller)}\n\n{BRIEF}{_brief_footer(caller)}") == BRIEF


def test_text_without_the_header_is_unchanged():
    # The user's own turns in a thread don't start with the mark; a footer-like tail stays.
    caller = _caller()
    own = f"Please look at this.{_brief_footer(caller)}"
    assert recall_query_text(own) == own
    assert recall_query_text("  ↳ From nowhere") == "  ↳ From nowhere"
    assert recall_query_text("") == ""


def test_prefetch_receives_the_brief_and_turn_start_keeps_the_message():
    caller = _caller()
    message = f"{_header(caller)}\n\n{BRIEF}{_brief_footer(caller)}"
    calls = {}

    class Manager:
        def on_turn_start(self, turn, text, **kw):
            calls["turn_start"] = text

        def prefetch_all(self, query, *, session_id=""):
            calls["prefetch"] = query
            return "recalled"

        def describe_recall(self):
            return ""

    agent = SimpleNamespace(_memory_manager=Manager(), _user_turn_count=1, session_id="child-1",
                            _emit_status=lambda *_: None)
    author = {"id": "bot:default", "name": "Ava", "is_bot": True}
    assert _memory_turn_start_and_prefetch(agent, message, author, "") == "recalled"
    assert calls["prefetch"] == BRIEF
    assert calls["turn_start"] == message


def test_header_only_follow_up_skips_recall():
    caller = _caller()
    called = []

    class Manager:
        def on_turn_start(self, *a, **kw):
            pass

        def prefetch_all(self, query, *, session_id=""):
            called.append(query)
            return "x"

    agent = SimpleNamespace(_memory_manager=Manager(), _user_turn_count=2, session_id="child-1",
                            _emit_status=lambda *_: None)
    assert _memory_turn_start_and_prefetch(agent, f"{_header(caller)}\n\nok", None, "") == ""
    assert called == []
