"""The ``thread`` tool and the parent wake against a real profile store (no model, no owner).

What a thread may be told to do, by whom, and when its result comes back are store questions, so
these drive the real schema: a login-stamped parent chat, thread launches bound to it and the
canonical admission ledger. The owner RPCs themselves (create, submit, steer, interrupt) are
exercised live; here ``_run`` is made to fail loudly, so a guard that let a call through to the
owner fails the test instead of passing quietly.
"""
import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_identity import authenticated_subject
from hermes_state import SessionDB
from hermes_state_terminal import RESULT_PREFIX, encode_result

DANNY = authenticated_subject({"provider": "basic", "user_id": "danny"})
MALLORY = authenticated_subject({"provider": "basic", "user_id": "mallory"})
MENU = """agent:
  name: Ava
delegation:
  backends:
    model_map:
      default: build
      options:
        - {name: fast, model: claude-haiku-5-5, provider: anthropic, reasoning_effort: medium, description: cheap}
        - {name: build, model: gpt-6.1-sol, provider: openai-codex, reasoning_effort: high, service_tier: fast}
    profile_routes:
      profiles:
        - {name: iris, description: QA}
        - {name: ghost, description: not a live profile}
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(MENU)
    iris = home / "profiles" / "iris"
    iris.mkdir(parents=True)
    (iris / "config.yaml").write_text("model:\n  default: claude-opus-5-5\n  provider: anthropic\n")
    with SessionDB(home / "state.db") as db:
        db.create_session("parent", "tui", user_id=DANNY)

    def no_owner(coro):
        coro.close()
        raise AssertionError("a guard let this call reach the owner")

    import tools.thread_tool as tool
    monkeypatch.setattr(tool, "_run", no_owner)
    return home


def _db(home):
    return SessionDB(home / "state.db")


def thread(db, sid, *, launcher="parent", user=DANNY):
    db.create_session(sid, "tui", user_id=user)
    db.record_launch(launch_id=f"thread:{sid}", kind="lane", launcher_profile="default",
                     launcher_session_id=launcher, child_profile="default")
    db.bind_launch(f"thread:{sid}", child_session_id=sid)


def admit(db, sid, aid, status, outcome=None, *, tool=True, reply=None):
    started = status == "started"
    db._write_sql(
        "INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, lineage_json, "
        "payload_json, payload_digest, intent, status, outcome, owner_epoch, generation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, ("thread-" if tool else "tui-") + aid, DANNY, sid, "[]", "{}", "d", "queue", status, outcome,
         1 if started else None, 1 if started else None))
    if reply is not None:
        db._write_sql("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?,?)",
                      (RESULT_PREFIX + aid, json.dumps(encode_result({"result": {"final_response": reply, "api_calls": 1}}))))


def settle(db, aid, outcome, reply=None):
    db._write_sql("UPDATE session_admissions SET status='terminal', outcome=? WHERE admission_id=?", (outcome, aid))
    if reply is not None:
        db._write_sql("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?,?)",
                      (RESULT_PREFIX + aid, json.dumps(encode_result({"result": {"final_response": reply}}))))


def call(sid="parent", **args):
    from tools.thread_tool import thread_tool
    return json.loads(thread_tool(args, parent_agent=SimpleNamespace(session_id=sid)))


def test_only_the_users_local_chats_can_lead_threads(home):
    with _db(home) as db:
        db.create_session("tg", "telegram", user_id="12345")
    out = call("tg", action="list")
    assert (out["success"], out["error"]) == (False, "caller_not_local")
    assert call(action="list") == {"success": True, "action": "list", "threads": []}


def test_writes_reach_only_this_chats_threads_and_reads_stay_with_the_user(home):
    with _db(home) as db:
        thread(db, "mine")
        db.create_session("solo", "tui", user_id=DANNY)
        db.append_message("solo", "user", "hello")
        db.create_session("theirs", "tui", user_id=MALLORY)
    for action, extra in (("send", {"message": "hi"}), ("stop", {}), ("settle", {}), ("rename", {"title": "x"})):
        out = call(action=action, thread_id="solo", **extra)
        assert out["error"] == "not_your_thread", (action, out)
    assert call(action="read", thread_id="solo")["messages"][0]["text"] == "hello"
    assert call(action="read", thread_id="theirs")["error"] == "thread_not_found"
    assert call(action="send", thread_id="theirs", message="hi")["error"] == "thread_not_found"
    # A thread is depth 1: it can't open threads, and it can't message the chat that opened it.
    assert call("mine", action="create", title="t", brief="b")["error"] == "depth_limit"
    assert call("mine", action="send", thread_id="parent", message="hi")["error"] == "not_your_thread"


def test_caps_refuse_before_anything_is_opened(home):
    from tools import thread_tool as tool
    with _db(home) as db:
        for n in range(tool.MAX_ACTIVE_PER_CHAT):
            thread(db, f"busy{n}")
            admit(db, f"busy{n}", f"a{n}", "started")
    out = call(action="create", title="one more", brief="b")
    assert (out["error"], "of this chat's threads" in out["message"]) == ("fanout_limit", True)

    with _db(home) as db:
        db.create_session("other", "tui", user_id=DANNY)
        for n in range(tool.MAX_ACTIVE_BOX - tool.MAX_ACTIVE_PER_CHAT):
            thread(db, f"elsewhere{n}", launcher="other")
            admit(db, f"elsewhere{n}", f"b{n}", "queued")
        for n in range(tool.MAX_ACTIVE_PER_CHAT):
            settle(db, f"a{n}", "completed")
        db.create_session("third", "tui", user_id=DANNY)
        for n in range(tool.MAX_ACTIVE_PER_CHAT):
            thread(db, f"third{n}", launcher="third")
            admit(db, f"third{n}", f"c{n}", "started")
    out = call(action="create", title="one more", brief="b")
    assert (out["error"], "across the box" in out["message"]) == ("fanout_limit", True)


def test_route_comes_from_the_delegation_menu(home):
    from tools.thread_tool import ThreadError, _caller, _route
    caller = _caller(SimpleNamespace(session_id="parent"))
    assert _route(caller, {"option": "fast"}) == {
        "profile": "default", "option": "fast", "model": "claude-haiku-5-5", "provider": "anthropic", "reasoning": "medium"}
    assert _route(caller, {"option": "build", "reasoning": "xhigh", "profile": "iris"})["reasoning"] == "xhigh"
    assert _route(caller, {"option": "build"})["service_tier"] == "fast"
    for bad in ({"option": "turbo"}, {"profile": "ghost"}, {"profile": "nobody"}, {"reasoning": "max"}):
        with pytest.raises(ThreadError):
            _route(caller, bad)
    menu = call(action="collaborators")
    assert [o["option"] for o in menu["options"]] == ["fast", "build"]
    assert menu["teammates"] == [{"profile": "iris", "for": "QA", "model": "claude-opus-5-5", "provider": "anthropic"}]


def test_wait_returns_the_tool_turn_not_the_users_and_hands_it_back_once(home):
    from tools import thread_state as ts
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "brief", "terminal", "completed", reply="the brief's result")
        admit(db, "t1", "danny", "terminal", "completed", tool=False, reply="Danny's own turn")
        thread(db, "t2")
        admit(db, "t2", "second", "terminal", "failed", reply="")
    with ts.reader(home) as conn:
        lineage = ts.lineage(conn, "parent")
    assert {i["admission_id"] for i in ts.settled_unreported(home, "default", lineage)} == {"brief", "second"}
    out = call(action="wait", thread_ids=["t1", "t2"], timeout_s=5)
    one, two = out["threads"]
    assert (one["status"], one["admission_id"], one["final_response"]) == ("done", "brief", "the brief's result")
    assert two["status"] == "failed" and "note" not in out
    assert ts.settled_unreported(home, "default", lineage) == []
    listed = {t["thread_id"]: t["status"] for t in call(action="list")["threads"]}
    assert listed == {"t1": "done", "t2": "failed"}


def test_thread_status_reads_the_ledger(home):
    from tools import thread_state as ts
    cases = [([("started", None)], "running"), ([("queued", None)], "queued"), ([("unknown", None)], "paused"),
             ([("terminal", "completed"), ("queued", None)], "queued"), ([("terminal", "cancelled")], "stopped"),
             ([("terminal", "interrupted")], "stopped"), ([], "idle")]
    for rows, expected in cases:
        assert ts.thread_status([{"status": s, "outcome": o} for s, o in rows]) == expected, rows


def _hooks(sent, *, accept=True):
    return dict(scope=lambda session: contextlib.nullcontext(), claim_turn=lambda session: True,
                release_turn=lambda session: None,
                submit=lambda rid, sid, session, text, what, **kw: sent.append(text) or accept)


def test_wake_delivers_each_result_once_and_retries_a_refused_submit(home):
    from tui_gateway import thread_notifications as tn
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="first result")
    session = {"session_key": "parent"}
    sent = []
    tn.poll("live", session, **_hooks(sent))
    tn.poll("live", session, **_hooks(sent))
    assert len(sent) == 1 and "[Thread update" in sent[0] and "first result" in sent[0], sent

    with _db(home) as db:
        admit(db, "t1", "a2", "unknown")
    tn.poll("live", session, **_hooks(sent))
    assert len(sent) == 2 and "paused" in sent[1], sent
    with _db(home) as db:
        settle(db, "a2", "completed", reply="second result")
    refused = []
    tn.poll("live", session, **_hooks(refused, accept=False))
    assert len(refused) == 1  # tried, refused: nothing is marked delivered
    tn.poll("live", session, **_hooks(sent))
    assert len(sent) == 3 and "second result" in sent[2], sent
    tn.poll("live", session, **_hooks(sent))
    assert len(sent) == 3


def test_wake_backs_off_for_a_chat_with_no_threads(home):
    from tui_gateway import thread_notifications as tn
    sent = []
    session = {"session_key": "parent"}
    tn.poll("live", session, **_hooks(sent))
    assert sent == [] and session["_thread_wake"]["next_scan"] > 0
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="done")
    tn.poll("live", session, **_hooks(sent))
    assert sent == []  # still backing off
    session["agent"] = SimpleNamespace(_thread_launched=True)  # a create in this process
    tn.poll("live", session, **_hooks(sent))
    assert len(sent) == 1


def test_threads_toolset_is_local_only():
    from toolsets import TOOLSETS, resolve_toolset
    from tools.delegate_tool_toolsets import DELEGATE_BLOCKED_TOOLS
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
    assert resolve_toolset("threads") == ["thread"]
    assert "thread" in TOOLSETS["hermes-cli"]["tools"]
    for bundle in ("hermes-cron", "hermes-telegram", "hermes-api-server", "hermes-acp", "hermes-webhook", "delegation"):
        assert "thread" not in TOOLSETS[bundle]["tools"], bundle
    assert "thread" in DELEGATE_BLOCKED_TOOLS
    assert "threads" in {key for key, *_ in CONFIGURABLE_TOOLSETS}


def test_a_paused_turn_is_cleared_only_when_it_is_the_tools(home):
    import asyncio
    from tools.thread_tool import ThreadError, _clear_paused, _resolve, _caller
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "mine", "unknown")
        thread(db, "t2")
        admit(db, "t2", "dannys", "unknown", tool=False)

    class Client:
        def __init__(self):
            self.calls = []

        async def rpc(self, method, **params):
            self.calls.append((method, params))
            return {}

    caller = _caller(SimpleNamespace(session_id="parent"))
    for sid in ("t1", "t2"):
        t = _resolve(caller, sid)
        rows = __import__("tools.thread_tool", fromlist=["_rows"])._rows(t)
        client = Client()
        if sid == "t1":
            assert asyncio.run(_clear_paused(client, t, rows, force=False)) == ["mine"]
            assert client.calls == [("prompt.resolve_unknown", {"session_id": "t1", "admission_id": "mine",
                                                                "execution_generation": None})]
        else:
            with pytest.raises(ThreadError) as refused:
                asyncio.run(_clear_paused(client, t, rows, force=False))
            assert refused.value.code == "thread_paused" and client.calls == []
            assert asyncio.run(_clear_paused(client, t, rows, force=True)) == ["dannys"]


def test_messages_into_threads_stop_until_the_user_writes_again(home):
    from tools import thread_tool as tool
    with _db(home) as db:
        db.append_message("parent", "user", "Danny: go")
        thread(db, "t1")
        for n in range(tool.MAX_SENDS_SINCE_USER):
            db.append_message("t1", "user", f"{tool.MARK} From Ava in parent \u00b7 thread tool\n\nmessage {n}")
        db.append_message("parent", "user", "[Thread update \u00b7 automatic, not from the user] ...")
    for action, extra in (("send", {"thread_id": "t1", "message": "one more"}), ("create", {"title": "t", "brief": "b"})):
        out = call(action=action, **extra)
        assert out["error"] == "check_in_with_user", out
    with _db(home) as db:
        db.append_message("parent", "user", "Danny: keep going")
    with pytest.raises(AssertionError, match="reach the owner"):  # past the guard, on its way to the owner
        call(action="send", thread_id="t1", message="one more")


def test_a_send_repeated_in_one_turn_is_one_send(home):
    from tools.thread_tool import _caller, _turn_mark
    caller = _caller(SimpleNamespace(session_id="parent"))
    with _db(home) as db:
        db.append_message("parent", "user", "first turn")
    first = _turn_mark(caller)
    assert _turn_mark(caller) == first
    with _db(home) as db:
        db.append_message("parent", "assistant", "working")
    assert _turn_mark(caller) == first
    with _db(home) as db:
        db.append_message("parent", "user", "second turn")
    assert _turn_mark(caller) > first


def test_a_claim_left_by_a_crash_is_retaken_and_a_delivered_result_is_not(home):
    from tools import thread_state as ts
    assert ts.claim(home, "a1", "done") and not ts.claim(home, "a1", "done")
    assert ts.claim(home, "a1", "done", now=time.time() + ts.CLAIM_STALE_S + 1)  # the first wake died
    ts.finish_claim(home, "a1", "done", accepted=True)
    assert ts.delivered(home, "a1", "done") and not ts.claim(home, "a1", "done", now=time.time() + 10_000)
    assert ts.claim(home, "a1", "paused")  # another transition of the same turn is its own delivery


def test_pending_thread_work_keeps_the_chat_resident(home):
    from tui_gateway import thread_notifications as tn
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "started")
    session = {"session_key": "parent"}
    sent = []
    tn.poll("live", session, **_hooks(sent))
    assert sent == [] and tn.owns_pending_thread_wake(session)
    session["_turn_cancel_requested"] = True
    assert not tn.owns_pending_thread_wake(session)  # after Stop nothing would deliver it anyway
    del session["_turn_cancel_requested"]
    with _db(home) as db:
        settle(db, "a1", "completed", reply="ok")
    tn.poll("live", session, **_hooks(sent))
    assert len(sent) == 1 and not tn.owns_pending_thread_wake(session)


def test_a_repeated_send_replays_the_first_and_touches_nothing(home):
    """Live regression: a repeated interrupt-send cancelled the very message the first one queued."""
    from tools import thread_tool as tool
    with _db(home) as db:
        db.append_message("parent", "user", "Danny: change course")
        thread(db, "t1")
        admit(db, "t1", "brief", "started")
    caller = tool._caller(SimpleNamespace(session_id="parent"))
    key = tool._key(None, "parent", "t1", "new plan", tool._turn_mark(caller))
    with _db(home) as db:
        db._write_sql(
            "INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, lineage_json, "
            "payload_json, payload_digest, intent, status) VALUES (?,?,?,?,?,?,?,?,?)",
            ("first-send", f"thread-send-{key}", DANNY, "t1", "[]", "{}", "d", "queue", "queued"))
    out = call(action="send", thread_id="t1", message="new plan", mode="interrupt")  # _run would fail loudly
    assert (out["reused"], out["admission_id"], out["status"]) == (True, "first-send", "queued")
