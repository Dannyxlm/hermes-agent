"""The ``thread`` tool and the parent wake against a real profile store (no model, no owner).

What a thread may be told to do, by whom, and when its result comes back are store questions, so
these drive the real schema: a login-stamped parent chat, thread launches bound to it and the
canonical admission ledger. By default ``_run`` fails loudly, so a guard that let a call through to
the owner fails the test instead of passing quietly; ``owner`` swaps in a fake owner that writes the
same rows the real one does, for the paths that must reach it (create, send, stop).
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
    assert {i["admission_id"] for i in ts.wake_state(home, "default", lineage)[0]} == {"brief", "second"}
    out = call(action="wait", thread_ids=["t1", "t2"], timeout_s=5)
    one, two = out["threads"]
    assert (one["status"], one["admission_id"], one["final_response"]) == ("done", "brief", "the brief's result")
    assert two["status"] == "failed" and "note" not in out
    assert ts.wake_state(home, "default", lineage)[0] == []
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
    session["_thread_wake"]["next_scan"] = 0.0  # the idle minute passed
    tn.poll("live", session, **_hooks(sent))
    assert len(sent) == 2 and "paused" in sent[1], sent
    with _db(home) as db:
        settle(db, "a2", "completed", reply="second result")
    refused = []
    tn.poll("live", session, **_hooks(refused, accept=False))
    assert len(refused) == 1  # tried, refused: nothing is marked delivered
    session["_thread_wake"]["next_scan"] = 0.0  # the refusal backoff passed
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
    assert len(sent) == 1 and session["agent"]._thread_launched is False  # one immediate scan, not every poll
    tn.poll("live", session, **_hooks(sent))
    assert session["_thread_wake"]["next_scan"] > time.monotonic()  # nothing out: back to the idle cadence


def test_a_refused_wake_backs_off_and_stops_pinning_a_chat_hosted_elsewhere(home):
    from tui_gateway import thread_notifications as tn
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
    session, refused = {"session_key": "parent"}, []
    for n in range(tn.REFUSALS_BEFORE_UNPIN):
        session["_thread_wake"] = {**session.get("_thread_wake", {}), "next_scan": 0.0}
        tn.poll("live", session, **_hooks(refused, accept=False))
        assert session["_thread_wake"]["next_scan"] > time.monotonic()  # not again on the next poll
    assert len(refused) == tn.REFUSALS_BEFORE_UNPIN and not tn.owns_pending_thread_wake(session)
    session["_thread_wake"]["next_scan"] = 0.0
    sent = []
    tn.poll("live", session, **_hooks(sent))
    assert len(sent) == 1 and session["_thread_wake"]["refusals"] == 0


def test_a_failed_claim_releases_the_turn_it_took(home, monkeypatch):
    from tools import thread_state as ts
    from tui_gateway import thread_notifications as tn
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ts, "claim", broken)
    released = []
    hooks = {**_hooks([]), "release_turn": released.append}
    session = {"session_key": "parent"}
    tn.poll("live", session, **hooks)
    assert released == [session]  # the chat isn't left busy


def test_threads_toolset_is_local_only_and_opt_in():
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
    from toolsets import TOOLSETS, TOOLSET_SESSION_PLATFORMS, resolve_toolset, session_platform_tool_drops
    from tools.delegate_tool_toolsets import DELEGATE_BLOCKED_TOOLS
    from tools.tool_search import is_deferrable_tool_name
    assert resolve_toolset("threads") == ["thread"]
    assert TOOLSET_SESSION_PLATFORMS["threads"] == frozenset({"cli", "tui", "desktop"})
    for platform in ("cli", "tui", "desktop"):
        assert "thread" not in session_platform_tool_drops(platform)
    for platform in ("telegram", "cron", "api_server", "acp", "webhook", None):
        assert "thread" in session_platform_tool_drops(platform), platform
    assert "thread" not in resolve_toolset("all")  # opt-in: only a config that names it gets it
    assert all("thread" not in spec["tools"] for name, spec in TOOLSETS.items() if name != "threads")
    assert "thread" in DELEGATE_BLOCKED_TOOLS
    assert "threads" in {key for key, *_ in CONFIGURABLE_TOOLSETS}
    assert not is_deferrable_tool_name("thread")  # a lead's tool is in hand, not behind tool_search


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


def test_automatic_turns_in_the_parent_are_not_the_user_writing(home):
    """A heartbeat, /loop, /goal or Kanban turn must not hand an unattended lead a fresh send budget."""
    from hermes_cli.goals import CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE, CONTINUATION_PROMPT_TEMPLATE
    from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE
    from hermes_cli.loops import WAKEUP_PROMPT_TEMPLATE, WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE
    from tools import thread_tool as tool
    for template in (HEARTBEAT_PROMPT_TEMPLATE, WAKEUP_PROMPT_TEMPLATE, WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
                     CONTINUATION_PROMPT_TEMPLATE, CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE):
        assert template.startswith(tool.AUTOMATIC_PREFIXES), template[:40]  # the headers the guard knows
    with _db(home) as db:
        db.append_message("parent", "user", "Danny: go")
        thread(db, "t1")
        for n in range(tool.MAX_SENDS_SINCE_USER):
            db.append_message("t1", "user", f"{tool.MARK} From Ava in parent \u00b7 thread tool\n\nmessage {n}")
        db.append_message("parent", "user", HEARTBEAT_PROMPT_TEMPLATE.format(interval="5m", prompt="check in"))
        db.append_message("parent", "user", WAKEUP_PROMPT_TEMPLATE.format(tick=3, cadence=" \u00b7 every 5m",
                                                                         prompt="watch the build"))
        db.append_message("parent", "user", "\u2714 Kanban t_1 done", display_metadata={"automatic_turn": "kanban"})
    assert call(action="send", thread_id="t1", message="one more")["error"] == "check_in_with_user"


def ts_reader(home):
    from tools import thread_state as ts
    return ts.reader(home)


class Owner:
    """A fake session owner: it writes the rows the real one does (sessions, launch binding,
    admissions) and records each RPC, so the tool's own sequencing is what's under test."""

    def __init__(self, home, *, refuse_titles=(), running=None):
        self.home, self.calls, self.revision, self.resumed = home, [], 0, []
        self.refuse_titles, self.running = set(refuse_titles), running

    async def rpc(self, method, **params):
        from hermes_cli.gateway_client import GatewayRPCError
        self.calls.append((method, params))
        self.revision += 1
        with _db(self.home) as db:
            if method == "session.create":
                sid = "t" + params["launch_id"][-6:]
                db.create_session(sid, "tui", user_id=DANNY)
                db.record_launch(launch_id=params["launch_id"], kind=params["launch_kind"],
                                 launcher_profile=params["started_by_profile"],
                                 launcher_session_id=params["started_by_session"], child_profile="default")
                db.bind_launch(params["launch_id"], child_session_id=sid)
                return {"session_id": sid, "info": {"title": "", "model": "m"}, "revision": self.revision}
            if method == "session.resume":
                self.resumed.append(self.revision)
                return {"revision": self.revision, "running": self.running is not None, "execution_generation": 1}
            if method == "session.mutate":
                if params["payload"].get("title") in self.refuse_titles:
                    raise GatewayRPCError("title_taken")
                return {"revision": self.revision}
            if method == "prompt.submit":
                aid, status = "adm-" + params["submission_id"][-8:], "queued" if self.running else "started"
                with ts_reader(self.home) as conn:  # the owner replays a request it already admitted
                    if conn.execute("SELECT 1 FROM session_admissions WHERE admission_id=?", (aid,)).fetchone():
                        return {"admission_id": aid, "status": "replayed"}
                admit(db, params["session_id"], aid, status)
                db._write_sql("UPDATE session_admissions SET request_id=? WHERE admission_id=?",
                              (params["submission_id"], aid))
                return {"admission_id": aid, "status": status}
            if method == "prompt.cancel":
                settle(db, params["admission_id"], "cancelled")
                return {"status": "terminal"}
        return {}


@pytest.fixture
def owner(home, monkeypatch):
    import asyncio
    import tools.thread_tool as tool
    fake = Owner(home)

    async def with_owner(home_, subject, fn):
        assert subject == DANNY  # every call acts for the caller's own principal
        return await fn(fake)

    monkeypatch.setattr(tool, "_run", asyncio.run)
    monkeypatch.setattr(tool, "_with_owner", with_owner)
    return fake


def test_create_opens_a_marked_thread_and_a_retry_in_the_same_turn_reports_how_it_stands(home, owner):
    owner.refuse_titles = {"\u21b3 Research"}  # the plain title is taken: the fallback must get a fresh revision
    with _db(home) as db:
        db.append_message("parent", "user", "Danny: research this")
    out = call(action="create", title="Research", brief="Find the facts.", option="fast")
    assert (out["success"], out["status"], out["option"]) == (True, "running", "fast"), out
    assert out["title"].startswith("\u21b3 Research \u00b7 ")
    methods = [m for m, _ in owner.calls]
    assert methods == ["session.create", "session.resume", "session.mutate", "session.resume", "session.mutate",
                       "prompt.submit"], methods
    first, second = (p for m, p in owner.calls if m == "session.mutate")
    assert (first["expected_revision"], second["expected_revision"]) == tuple(owner.resumed)
    create = owner.calls[0][1]
    assert (create["model"], create["provider"], create["reasoning"]) == ("claude-haiku-5-5", "anthropic", "medium")
    brief = owner.calls[-1][1]
    assert brief["finite"] is True and brief["turn_author"]["is_bot"] is True
    assert brief["text"].startswith("\u21b3 From Ava in") and "Find the facts." in brief["text"]

    with _db(home) as db:
        settle(db, out["admission_id"], "completed", reply="facts")
    again = call(action="create", title="Research", brief="Find the facts.", option="fast")
    assert (again["reused"], again["thread_id"], again["status"]) == (True, out["thread_id"], "done")
    with _db(home) as db:
        db.append_message("parent", "user", "Danny: once more")
    later = call(action="create", title="Research", brief="Find the facts.", option="fast")
    assert "reused" not in later and later["thread_id"] != out["thread_id"]  # a later turn is new work


def test_stop_cancels_and_interrupts_only_the_tools_turns_and_wakes_no_one_about_it(home, owner):
    from tui_gateway import thread_notifications as tn
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "running", "started")
        admit(db, "t1", "queued1", "queued")
        admit(db, "t1", "dannys", "queued", tool=False)
    owner.running = "running"
    out = call(action="stop", thread_id="t1")
    assert (out["status"], out["cancelled_queued"]) == ("stopping", ["queued1"]), out
    assert [m for m, _ in owner.calls] == ["session.resume", "prompt.cancel", "session.interrupt"]
    with _db(home) as db:
        settle(db, "running", "interrupted")
    sent = []
    tn.poll("live", {"session_key": "parent"}, **_hooks(sent))
    assert sent == []  # the lead stopped them itself: no update telling it so


def test_interrupt_send_cancels_then_interrupts_then_queues_and_leaves_the_users_turn(home, owner):
    with _db(home) as db:
        db.append_message("parent", "user", "Danny: change course")
        thread(db, "t1")
        admit(db, "t1", "running", "started")
        admit(db, "t1", "queued1", "queued")
    owner.running = "running"
    out = call(action="send", thread_id="t1", message="New plan: X, because Y.", mode="interrupt")
    assert out["note"].startswith("Interrupted") and out["status"] == "queued", out
    assert [m for m, _ in owner.calls] == ["session.resume", "prompt.cancel", "session.interrupt", "prompt.submit"]

    with _db(home) as db:
        thread(db, "t2")
        admit(db, "t2", "dannys", "started", tool=False)
    owner.calls.clear()
    out = call(action="send", thread_id="t2", message="New plan.", mode="interrupt")
    assert [m for m, _ in owner.calls] == ["session.resume", "prompt.submit"]  # the user's turn runs on
    assert "user's own turn" in out["note"]


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
