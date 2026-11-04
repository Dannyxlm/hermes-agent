"""The ``thread`` tool and the parent wake against a real profile store (no model, no owner).

What a thread may be told to do, by whom, and when its result comes back are store questions, so
these drive the real schema: a login-stamped parent chat, thread launches bound to it and the
canonical admission ledger. By default ``_run`` fails loudly, so a guard that let a call through to
the owner fails the test instead of passing quietly; ``owner`` swaps in a fake owner that writes the
same rows the real one does, for the paths that must reach it (create, send, stop).
"""
import contextlib
import hashlib
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


def test_host_owner_local_rows_can_lead_threads(home):
    from gateway.owner_identity import host_owner_subject
    from tools.thread_tool import _subject

    owner = host_owner_subject()
    with _db(home) as db:
        db.create_session('native', 'gui', user_id=owner)
        db.create_session('foreign-os', 'gui', user_id='uid:99999999')
        for source in ('telegram', 'cron', 'subagent'):
            db.create_session(source, source, user_id=owner)
    assert _subject(home, 'native') == owner
    assert call('native', action='list')['success'] is True
    assert _subject(home, 'foreign-os') is None
    for source in ('telegram', 'cron', 'subagent'):
        assert _subject(home, source) is None


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
    """Heartbeat, /loop, /goal, process and delegation notices, compaction rows, scheduled wakes and Kanban
    notices must not hand an unattended lead a fresh send budget; the user's own messages (typed, mid-turn,
    with an attachment) do reset it."""
    from agent.context_compressor import _BACKGROUND_PROCESS_NOTIFICATION_PREFIX, SUMMARY_PREFIX
    from agent.i18n import t
    from cron.session_delivery import wake_message
    from gateway.thread_wake import render
    from hermes_cli.goals import CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE, CONTINUATION_PROMPT_TEMPLATE
    from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE
    from hermes_cli.loops import WAKEUP_PROMPT_TEMPLATE, WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE
    from tools import thread_tool as tool
    from tools.process_registry_notifications import _format_async_delegation
    delegation = _format_async_delegation({"delegation_id": "deleg_1", "goal": "g", "completed_at": 0,
                                           "results": [{"status": "completed", "summary": "ok", "task_index": 0}]})
    kanban = [t("gateway.kanban.ping.head", lang=lang, board_tag="", assignee_tag="@iris ", task_id="t_1a2b") + " done"
              for lang in ("en", "zh")]
    automatic = [HEARTBEAT_PROMPT_TEMPLATE.format(interval="5m", prompt="check in"),
                 WAKEUP_PROMPT_TEMPLATE.format(tick=3, cadence=" \u00b7 every 5m", prompt="watch the build"),
                 WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE, CONTINUATION_PROMPT_TEMPLATE, CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE,
                 wake_message({"name": "job"}, "tick"), render([], [], "default"),
                 _BACKGROUND_PROCESS_NOTIFICATION_PREFIX + "proc_1 completed (exit 0).", delegation,
                 SUMMARY_PREFIX + "\n...", *kanban]
    for text in automatic[:-len(kanban)]:
        assert text.startswith("["), text[:40]  # every automatic template keeps its bracketed header
    with _db(home) as db:
        db.append_message("parent", "user", "Danny: go")
        thread(db, "t1")
        for n in range(tool.MAX_SENDS_SINCE_USER):
            db.append_message("t1", "user", f"{tool.MARK} From Ava in parent \u00b7 thread tool\n\nmessage {n}")
        for text in automatic:
            db.append_message("parent", "user", text)
    assert call(action="send", thread_id="t1", message="one more")["error"] == "check_in_with_user"
    for human, kind in (("keep going", None), ("[OUT-OF-BAND USER MESSAGE \u2014 ...]\nchange course", "steer"),
                        ("[The user attached an image] look", None)):
        with _db(home) as db:
            db.append_message("parent", "user", human, display_kind=kind)
        with pytest.raises(AssertionError, match="reach the owner"):  # past the guard, on its way to the owner
            call(action="send", thread_id="t1", message=f"one more after {human[:8]}")
        with _db(home) as db:
            db.append_message("parent", "user", "[IMPORTANT: Background process proc_2 completed (exit 0).")
            db._write_sql("UPDATE messages SET timestamp=timestamp-10000 WHERE session_id='parent' AND content=?", (human,))


def ts_reader(home):
    from tools import thread_state as ts
    return ts.reader(home)


class Owner:
    """A fake session owner: it writes the rows the real one does (sessions, launch binding,
    admissions) and records each RPC, so the tool's own sequencing is what's under test."""

    def __init__(self, home, *, refuse_titles=(), running=None):
        self.home, self.calls, self.revision, self.resumed = home, [], 0, []
        self.refuse_titles, self.running = set(refuse_titles), running
        self.refuse_submit = self.lose_receipt = self.lose_resolve_receipt = False
        self.update_outcome = "completed"  # how a thread update's turn ends: an outcome, "unknown" or None (running)
        self.attached = set()  # like the real owner, writes are taken only for an id this connection attached

    def _attach(self, sid):
        """What the owner's resume attaches: a local chat's logical id, otherwise the compression tip."""
        from tools import thread_state as ts
        with ts_reader(self.home) as conn:
            logical = sorted(i for i in ts.lineage(conn, sid) if i.startswith("local-"))
            target = logical[0] if logical else ts.tip(conn, sid)
        self.attached.add(target)
        return target

    def submits(self, prefix="thread_update:"):
        return [p for m, p in self.calls if m == "prompt.submit" and p["submission_id"].startswith(prefix)]

    async def rpc(self, method, **params):
        from hermes_cli.gateway_client import GatewayRPCError
        self.calls.append((method, params))
        self.revision += 1
        writes = {"prompt.submit", "prompt.cancel", "prompt.resolve_unknown", "session.interrupt", "session.mutate"}
        if method in writes and params["session_id"] not in self.attached:
            raise GatewayRPCError("permission_denied")
        with _db(self.home) as db:
            if method == "session.create":
                sid = "t" + params["launch_id"][-6:]
                db.create_session(sid, "tui", user_id=DANNY)
                db.record_launch(launch_id=params["launch_id"], kind=params["launch_kind"],
                                 launcher_profile=params["started_by_profile"],
                                 launcher_session_id=params["started_by_session"], child_profile="default")
                db.bind_launch(params["launch_id"], child_session_id=sid)
                self.attached.add(sid)
                return {"session_id": sid, "info": {"title": "", "model": "m"}, "revision": self.revision}
            if method == "session.resume":
                self.resumed.append(self.revision)
                return {"session_id": self._attach(params["session_id"]), "revision": self.revision,
                        "running": self.running is not None, "execution_generation": 1}
            if method == "session.mutate":
                if params["payload"].get("title") in self.refuse_titles:
                    raise GatewayRPCError("title_taken")
                return {"revision": self.revision}
            if method == "prompt.submit":
                return self._submit(db, params)
            if method == "prompt.cancel":
                settle(db, params["admission_id"], "cancelled")
                return {"status": "terminal"}
            if method == "prompt.resolve_unknown":
                settle(db, params["admission_id"], "interrupted")  # idempotent on a row it already settled
                if self.lose_resolve_receipt:
                    self.lose_resolve_receipt = False
                    raise ConnectionError("receipt lost")
                return {"status": "terminal"}
        return {}

    def _submit(self, db, params):
        from hermes_cli.gateway_client import GatewayRPCError
        if self.refuse_submit:
            raise GatewayRPCError("owner_busy")
        digest = hashlib.sha256(json.dumps({k: v for k, v in params.items() if k != "submission_id"},
                                           sort_keys=True).encode()).hexdigest()
        with ts_reader(self.home) as conn:  # the owner replays a request id only for the identical payload
            seen = conn.execute("SELECT admission_id, payload_digest FROM session_admissions WHERE request_id=?",
                                (params["submission_id"],)).fetchone()
        if seen:
            if seen["payload_digest"] != digest:
                raise GatewayRPCError("admission_conflict")
            return {"admission_id": seen["admission_id"], "status": "replayed"}
        aid, status = "adm-" + params["submission_id"][-8:], "queued" if self.running else "started"
        admit(db, params["session_id"], aid, status)
        db._write_sql("UPDATE session_admissions SET request_id=?, payload_digest=? WHERE admission_id=?",
                      (params["submission_id"], digest, aid))
        if params["submission_id"].startswith("thread_update:") and self.update_outcome == "unknown":
            db._write_sql("UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (aid,))
        elif params["submission_id"].startswith("thread_update:") and self.update_outcome:
            settle(db, aid, self.update_outcome)  # the update turn ran
        if self.lose_receipt:
            self.lose_receipt = False
            raise ConnectionError("receipt lost")
        return {"admission_id": aid, "status": status}


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
    from gateway.thread_wake import deliver
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
    assert not deliver(home, "default", "parent") and owner.submits() == []  # it stopped them itself: no update


def test_the_owner_wakes_an_idle_lead_once_with_every_result(home, owner):
    from gateway.thread_wake import SUBMISSION_PREFIX, deliver
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="first result")
        thread(db, "t2")
        admit(db, "t2", "a2", "terminal", "failed", reply="")
        admit(db, "t2", "dannys", "terminal", "completed", tool=False, reply="Danny's own turn")
        thread(db, "t3")
        admit(db, "t3", "a3", "started")
    assert deliver(home, "default", "parent")
    (update,) = owner.submits()
    assert (update["session_id"], update["queued"], update["finite"], update["turn_author"]["is_bot"]) == (
        "parent", True, True, True)
    assert update["submission_id"].startswith(SUBMISSION_PREFIX) and not update["submission_id"].startswith("thread-")
    text = update["text"]
    assert text.startswith("[Thread update") and "first result" in text and "failed" in text, text
    assert "Danny's own turn" not in text and "Still working: " in text  # the user's turns aren't reported back
    assert not deliver(home, "default", "parent") and len(owner.submits()) == 1  # each result once
    with _db(home) as db:
        settle(db, "a3", "completed", reply="third result")
    assert deliver(home, "default", "parent") and "third result" in owner.submits()[-1]["text"]


def test_a_busy_lead_gets_its_update_when_idle(home, owner):
    from gateway.thread_wake import deliver
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
        admit(db, "parent", "leads-turn", "started", tool=False)
    assert not deliver(home, "default", "parent") and owner.calls == []  # mid-turn: maybe waiting on it right now
    with _db(home) as db:
        settle(db, "leads-turn", "completed")
    assert deliver(home, "default", "parent")


def test_a_refused_or_lost_update_is_retried_as_the_same_admission(home, owner):
    """The submit is refused, then admitted with its receipt lost, while another thread settles in between: the
    retry carries the identical payload under the same key (the owner's digest rule would refuse a drifted one),
    no second turn re-reports anything, and the newly settled result comes in the next update."""
    from gateway.thread_wake import deliver
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="first")
        thread(db, "t2")
        admit(db, "t2", "a2", "started")
    owner.refuse_submit = True
    with pytest.raises(Exception, match="owner_busy"):
        deliver(home, "default", "parent")
    with _db(home) as db:
        settle(db, "a2", "completed", reply="second")  # the still-working list the first text named is stale now
    owner.refuse_submit, owner.lose_receipt = False, True  # admitted this time, but the reply never came back
    with pytest.raises(ConnectionError):
        deliver(home, "default", "parent")
    assert not deliver(home, "default", "parent")  # the ledger shows it admitted and done: no resubmit
    first, retry = owner.submits()
    assert first == retry and "first" in first["text"] and "second" not in first["text"]
    assert deliver(home, "default", "parent") and "second" in owner.submits()[-1]["text"]
    assert "first" not in owner.submits()[-1]["text"].replace("first message", "")  # reported once
    with ts_reader(home) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_admissions WHERE request_id LIKE 'thread_update:%'"
                            ).fetchone()[0] == 2


def test_an_update_cut_off_by_a_restart_is_cleared_and_sent_again(home, owner):
    """A restart during the update's turn leaves it ``unknown``, which parks the lead. Nobody but the sweeper
    sent it, so a fresh sweeper (the restarted owner) discards it and sends it again; the results are delivered
    only once a turn carrying them is done."""
    from gateway import thread_wake
    from tools import thread_state as ts
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
    owner.update_outcome = "unknown"
    assert thread_wake.deliver(home, "default", "parent")
    assert not ts.delivered(home, "a1", "done")  # admitted is not delivered
    owner.update_outcome = "completed"
    sweeper = thread_wake.Sweeper(lambda: [home])  # the restarted owner: everything it knows is on disk
    assert sweeper.sweep() == 0  # saves the resend, naming the cut-off turn...
    assert sweeper.sweep() == 0  # ...discards that turn...
    (cleared,) = [p for m, p in owner.calls if m == "prompt.resolve_unknown"]
    assert cleared["admission_id"] == "adm-" + owner.submits()[0]["submission_id"][-8:]
    assert sweeper.sweep() == 1  # ...then sends it again under a new key
    first, again = owner.submits()
    assert again["submission_id"] != first["submission_id"] and again["text"] == first["text"] + thread_wake.RESENT
    assert sweeper.sweep() == 0 and ts.delivered(home, "a1", "done")
    assert len(owner.submits()) == 1 + 1


def _compress(db, sid, into):
    db.create_session(into, "tui", user_id=DANNY)
    db._write_sql("UPDATE sessions SET parent_session_id=? WHERE id=?", (sid, into))
    db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id=?", (sid,))


def _update_admission(home):
    with ts_reader(home) as conn:
        return conn.execute("SELECT admission_id FROM session_admissions WHERE request_id LIKE 'thread_update:%' "
                            "ORDER BY seq DESC").fetchone()[0]


def test_a_compressed_local_lead_gets_its_update_at_the_id_the_owner_attaches(home, owner):
    """A local chat's continuations are timestamp ids; the owner attaches its logical ``local-`` id and refuses
    writes to any other."""
    from gateway.thread_wake import deliver
    with _db(home) as db:
        db.create_session("local-lead", "tui", user_id=DANNY)
        thread(db, "t1", launcher="local-lead")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
        _compress(db, "local-lead", "20261031_120000_abcdef")
    assert deliver(home, "default", "local-lead")
    assert [u["session_id"] for u in owner.submits()] == ["local-lead"]


def test_a_compressed_local_thread_takes_sends_at_its_logical_id(home, owner):
    with _db(home) as db:
        thread(db, "local-t1")
        _compress(db, "local-t1", "20261031_120000_abcdef")
    out = call(action="send", thread_id="local-t1", message="carry on")
    assert out.get("admission_id"), out
    (sent,) = owner.submits(prefix="thread-send-")
    assert sent["session_id"] == "local-t1"


def test_a_lead_that_compresses_mid_update_keeps_one_update(home, owner):
    """The pending update is keyed on the conversation's first segment, so a compression during the update's
    long turn (its claims long stale by then) finds the same record instead of sending the results twice."""
    import os
    from gateway.thread_wake import deliver
    from tools import thread_state as ts
    with _db(home) as db:
        db.create_session("local-lead", "tui", user_id=DANNY)
        thread(db, "t1", launcher="local-lead")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
    owner.update_outcome = None  # the update's turn is still running...
    assert deliver(home, "default", "local-lead")
    with _db(home) as db:
        _compress(db, "local-lead", "20261031_120000_abcdef")  # ...when the lead compresses
    for path in ts._marker_dir(home).glob("*.claim"):
        os.utime(path, (time.time() - 3600, time.time() - 3600))
    assert not deliver(home, "default", "local-lead")
    with _db(home) as db:
        settle(db, _update_admission(home), "completed")
    assert not deliver(home, "default", "local-lead") and ts.delivered(home, "a1", "done")
    assert not deliver(home, "default", "local-lead") and len(owner.submits()) == 1


def test_a_resend_survives_a_crash_between_clearing_and_saving(home, owner):
    """The resend's intent is saved before the cut-off turn is cleared: losing the clear's answer (a crash right
    after it) must not turn the cleared turn into "delivered"."""
    from gateway.thread_wake import RESENT, deliver
    from tools import thread_state as ts
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
    owner.update_outcome = "unknown"
    assert deliver(home, "default", "parent")
    owner.update_outcome = "completed"
    assert not deliver(home, "default", "parent")  # saves the resend, cut-off turn named
    owner.lose_resolve_receipt = True
    with pytest.raises(ConnectionError):
        deliver(home, "default", "parent")  # clears it, then "crashes"
    assert not ts.delivered(home, "a1", "done")
    assert not deliver(home, "default", "parent")  # clears again (idempotent), forgets the cut-off turn
    assert deliver(home, "default", "parent") and owner.submits()[-1]["text"].endswith(RESENT)
    assert not deliver(home, "default", "parent") and ts.delivered(home, "a1", "done")


def test_a_cancelled_update_is_sent_again_a_bounded_number_of_times(home, owner):
    from gateway.thread_wake import MAX_RESENDS, deliver
    from tools import thread_state as ts
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
    owner.update_outcome = "cancelled"  # cancelled before it ran: nobody has read it
    for _ in range(4 * (MAX_RESENDS + 2)):
        deliver(home, "default", "parent")
    assert len(owner.submits()) == 1 + MAX_RESENDS and ts.delivered(home, "a1", "done")


def test_giving_up_on_a_cut_off_update_leaves_the_lead_unparked(home, owner):
    from gateway.thread_wake import MAX_RESENDS, deliver
    from tools import thread_state as ts
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
    owner.update_outcome = "unknown"  # every copy is cut off by a restart
    for _ in range(6 * (MAX_RESENDS + 2)):
        deliver(home, "default", "parent")
    assert len(owner.submits()) == 1 + MAX_RESENDS and ts.delivered(home, "a1", "done")
    with ts_reader(home) as conn:
        assert not conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id='parent' "
                                "AND status='unknown'").fetchone()


def test_a_lead_parked_on_its_users_cut_off_turn_waits_for_them(home, owner):
    from gateway.thread_wake import deliver
    with _db(home) as db:
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="result")
        admit(db, "parent", "dannys", "unknown", tool=False)
    assert not deliver(home, "default", "parent") and owner.calls == []  # his to discard, not the sweeper's
    with _db(home) as db:
        settle(db, "dannys", "interrupted")  # he discarded it (or closed the chat)
    assert deliver(home, "default", "parent")


def test_the_sweep_serves_every_lead_and_backs_off_one_that_fails(home, owner, monkeypatch):
    from gateway import thread_wake
    old = time.time() - 10 * 86400
    with _db(home) as db:
        db.create_session("other", "tui", user_id=DANNY)
        thread(db, "t1")
        admit(db, "t1", "a1", "terminal", "completed", reply="for parent")
        thread(db, "t2", launcher="other")
        admit(db, "t2", "a2", "terminal", "completed", reply="for other")
        db.create_session("old", "tui", user_id=DANNY)
        thread(db, "t3", launcher="old")
        admit(db, "t3", "a3", "terminal", "completed", reply="stale")
        db.create_session("compressed", "tui", user_id=DANNY)
        thread(db, "t4", launcher="compressed")  # compressed; its continuation finished today
        db.create_session("t4b", "tui", user_id=DANNY)
        db.create_session("gone", "tui", user_id=DANNY)
        thread(db, "t5", launcher="gone")
        admit(db, "t5", "a5", "terminal", "completed", reply="archived lead")
        db._write_sql("UPDATE sessions SET archived=1 WHERE id='gone'")
        db._write_sql("UPDATE sessions SET parent_session_id='t4' WHERE id='t4b'")
        db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='t4'")
        db._write_sql("UPDATE sessions SET started_at=?, last_activity_at=? WHERE id IN ('t3','t4')", (old, old))
    assert thread_wake.leads("default") == ["compressed", "gone", "other", "parent"]  # t3 idle past the window
    real = thread_wake.deliver

    def flaky(home_, profile, lead):
        if lead == "other":
            raise RuntimeError("owner unreachable")
        return real(home_, profile, lead)

    monkeypatch.setattr(thread_wake, "deliver", flaky)
    sweeper = thread_wake.Sweeper(lambda: [home])
    assert sweeper.sweep(now=1000.0) == 1  # parent; the archived lead gets nothing; the failing one backs off
    assert sweeper._retry[(home.resolve(), "other")][0] == 1000.0 + thread_wake.BACKOFF_S
    monkeypatch.setattr(thread_wake, "deliver", real)
    assert sweeper.sweep(now=1001.0) == 0  # still backing off
    assert sweeper.sweep(now=1000.0 + thread_wake.BACKOFF_S) == 1 and (home.resolve(), "other") not in sweeper._retry
    assert [p["session_id"] for p in owner.submits()] == ["parent", "other"]


def test_the_sweeper_sweeps_until_stopped():
    import threading
    from gateway.thread_wake import Sweeper
    swept_twice = threading.Event()

    class Counting(Sweeper):
        swept = 0

        def sweep(self, now=None):
            Counting.swept += 1
            if Counting.swept >= 2:
                swept_twice.set()
            return 0

    stop = threading.Event()
    worker = threading.Thread(target=Counting(list).run, args=(stop, 0.01))
    worker.start()
    assert swept_twice.wait(timeout=5)  # it keeps sweeping on its interval...
    stop.set()
    worker.join(timeout=2)
    assert not worker.is_alive()  # ...and its loop ends with the stop


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


def test_the_agent_loop_hands_thread_its_own_agent(home):
    """Live regression (release 20261031-threads): the registry handler read kw["parent_agent"], which
    only intercepted tools get, so every call from a real chat failed with no_session."""
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext, resolve_invoke_tool_executor
    agent = SimpleNamespace(session_id="parent", enabled_toolsets=["terminal", "threads"])
    assert resolve_invoke_tool_executor(agent, "thread") is INLINE_TOOL_EXECUTORS["thread"]  # concurrent path
    out = json.loads(INLINE_TOOL_EXECUTORS["thread"](agent, {"action": "list"}, InlineToolContext("task", "call_1")))
    assert out == {"success": True, "action": "list", "threads": []}


def test_a_registry_dispatch_names_the_chat_by_its_session_id(home):
    from tools.registry import registry
    out = json.loads(registry.dispatch("thread", {"action": "list"}, task_id="task", session_id="parent"))
    assert out["success"] is True, out
    assert json.loads(registry.dispatch("thread", {"action": "list"}, task_id="task"))["error"] == "no_session"


def test_no_registry_tool_expects_an_agent_it_is_never_given():
    """A registry handler gets task_id/session_id/user_task. A tool that needs ``parent_agent`` must run
    inline (agent/inline_tool_executors.py) or be intercepted by the agent loop (model_tools)."""
    import inspect
    import model_tools  # registers every tool
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS
    from model_tools import _AGENT_LOOP_TOOLS
    from tools.registry import registry
    agent_level = set(INLINE_TOOL_EXECUTORS) | set(_AGENT_LOOP_TOOLS)
    for name, entry in registry._tools.items():
        try:
            source = inspect.getsource(entry.handler)
        except (OSError, TypeError):
            continue
        if 'kw.get("parent_agent")' in source or "kwargs.get(\"parent_agent\")" in source:
            assert name in agent_level, f"{name} reads parent_agent from registry kwargs, which never carry it"


def test_gateway_housekeeping_runs_the_sweeper_inside_its_own_lifetime(home, monkeypatch):
    """The owner's sweeper starts with housekeeping (a runtime writer) and is stopped and joined before it returns."""
    import gateway.run as gateway_run
    from gateway import thread_wake

    class Ticks:  # one housekeeping tick, no sleeping
        def __init__(self):
            self.n = 0

        def is_set(self):
            return self.n >= 1

        def wait(self, timeout=None):
            self.n += 1
            return True

    started = {}

    class Worker:
        def join(self, timeout=None):
            started["joined_after_stop"] = started["stop"].is_set()

    def fake_start(stop, homes):
        started.update(stop=stop, homes=homes())
        return Worker()

    monkeypatch.setattr(thread_wake, "start", fake_start)
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False), adapters=None)
    gateway_run._start_gateway_housekeeping(Ticks(), interval=0, runner=runner)
    assert started["homes"] == [home] and started["joined_after_stop"] is True
