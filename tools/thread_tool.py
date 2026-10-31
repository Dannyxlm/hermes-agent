"""Threads: lead parallel work through the user's own visible chats.

``thread`` opens chats ("threads") with a brief, messages them (queued, or interrupting the turn
it sent), waits on them, reads them, lists, renames, settles (archives) and stops them, and offers
the collaborator menu (models, reasoning, teammate profiles). It is a client of the canonical
session owner, the same RPCs Desktop, the phone and ``hermes chat`` use (``session.create`` with
launch provenance, ``session.mutate``, ``prompt.submit``, ``prompt.cancel``,
``prompt.resolve_unknown``, ``session.interrupt``), plus read-only store reads
(``tools/thread_state.py``). There is deliberately no mid-turn steer: ``session.steer`` delivers its
text inside the marker that grants the user's own authority, and an agent must not speak with it. There is no
orchestration store: each thread is a ``work_launches`` row (``launch_id`` ``thread:<key>``) bound
to the chat that opened it, so Family shows it as that chat's child. When a thread finishes work
the tool sent it, the dashboard wakes the parent chat with the result
(``tui_gateway/thread_notifications.py``).

Guards. These are rails for a cooperative agent, not a security boundary: anything with a
terminal can call the same RPCs as the same user. The caller must be a local chat whose row
resolves to a login principal (the ``cron.session_delivery`` rule), and every thread is a local
chat of that principal, so nothing here reaches a messaging platform, a client or a member.
Writes (send, rename, settle, stop) reach only the caller's own threads; reads and waits
reach any of the principal's local chats. Threads can't open threads (depth 1); caps bound
running threads per chat and box-wide (counted under a box-wide file lock) and creates per hour;
and the messages one chat sends into its threads stop at a fixed number until the user writes in
that chat again, so a wake-and-send loop can't run unattended. Every message carries a visible mark
naming the sending agent and chat; tool turns are ``finite`` (a clarify inside one is answered
headlessly), and the user's own turns in a thread are never interrupted, stopped or discarded
without ``force``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from tools import thread_state as ts
from tools.registry import registry

MARK = ts.MARK
MAX_ACTIVE_PER_CHAT = 6
MAX_ACTIVE_BOX = 12
MAX_CREATES_PER_HOUR = 20
#: Creates and messages one chat sends into its threads before the user writes there again.
MAX_SENDS_SINCE_USER = 40
WAIT_DEFAULT_S = 120
WAIT_MAX_S = 300  # under the executor's 420 s tool deadline
POLL_S = 2.0
TITLE_MAX = 60
TEXT_MAX = 100_000
MESSAGE_CAP, MESSAGE_CAP_FULL = 3_000, 12_000
FINAL_CAP, FINAL_CAP_MANY = 12_000, 3_000
PREVIEW_CAP = 200
READ_DEFAULT, READ_MAX = 10, 50
WAIT_MANY_MAX = 12
RUN_BUDGET_DEFAULT_S, RUN_BUDGET_MIN_S, RUN_BUDGET_MAX_S = 7_200, 60, 28_800
REASONING = ["none", "minimal", "low", "medium", "high", "xhigh"]
ACTIVE = {"running", "queued", "paused"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: User-role rows no human typed, by their fixed headers (the same on every surface; a test pins each to
#: its template): thread updates, scheduled wakes, /heartbeat and /loop ticks, /goal continuations.
AUTOMATIC_PREFIXES = ("[Thread update", "[Scheduled wake", "[Heartbeat \u2014 recurring instruction",
                      "[/loop wakeup #", "[Continuing toward your standing goal")
#: ``display_metadata`` key on automatic turns that have no fixed header (Kanban notices).
AUTOMATIC_KEY = "automatic_turn"
# Parallel tool calls in one turn run on worker threads of this process: one cap check + create at a time.
_CREATE_LOCK = threading.Lock()


class ThreadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Caller:
    session_id: str
    lineage: frozenset
    profile: str
    home: Path
    subject: str
    title: str
    cwd: str | None
    name: str
    toolsets: frozenset | None
    launch: dict | None  # set when the caller is itself a thread


@dataclass(frozen=True)
class Thread:
    profile: str
    home: Path
    session_id: str  # the live tip of its compression chain
    lineage: frozenset
    title: str
    model: str | None
    launch: dict | None
    archived: bool

    def handle(self, caller: Caller) -> str:
        return _handle(caller, self.profile, self.session_id)


def _handle(caller: Caller, profile: str, session_id: str) -> str:
    return session_id if profile == caller.profile else f"{profile}:{session_id}"


# ── who is calling, and which chat is meant ───────────────────────────────────────────────────

def _subject(home: Path, session_id: str):
    """The login principal a local chat acts for, or None (messaging, cron, subagent, socket-uid rows)."""
    from cron.session_delivery import _wake_subject
    try:
        return _wake_subject(home, session_id)
    except (sqlite3.Error, OSError):  # an unreadable store proves no principal
        return None


def _config() -> dict:
    from hermes_cli.config import load_config
    return load_config() or {}


def _caller(parent_agent) -> Caller:
    from hermes_constants import get_hermes_home
    sid = getattr(parent_agent, "session_id", None)
    if not isinstance(sid, str) or not sid:
        raise ThreadError("no_session", "Thread tools need a chat session.")
    home = get_hermes_home().resolve()
    subject = _subject(home, sid)
    if subject is None:
        raise ThreadError("caller_not_local", (
            "Thread tools work only from the user's own local chats (Desktop, TUI, the phone app or a thread). "
            "This session (messaging, scheduled or a subagent) can't use them."))
    profile = ts.profile_of(home)
    with ts.reader(home) as conn:
        if conn is None:
            raise ThreadError("no_session", "This profile has no session store yet.")
        lineage = ts.lineage(conn, sid)
        row = conn.execute("SELECT title, cwd FROM sessions WHERE id=?", (sid,)).fetchone()
        launch = ts.launch_of(conn, profile, lineage)
    name = ((_config().get("agent") or {}).get("name") or "").strip() or "Hermes"
    enabled = getattr(parent_agent, "enabled_toolsets", None)
    toolsets = frozenset(enabled) if isinstance(enabled, (list, tuple, set, frozenset)) else None
    return Caller(sid, lineage, profile, home, subject, ((row["title"] if row else "") or "").strip(),
                  (row["cwd"] if row else None) or None, name, toolsets, launch)


def _resolve(caller: Caller, handle) -> Thread:
    """One of the caller principal's local chats, by handle (``<id>`` or ``<profile>:<id>``)."""
    if not isinstance(handle, str) or not handle.strip():
        raise ThreadError("invalid_request", "thread_id is required.")
    handle = handle.strip()
    homes = ts.homes()
    profile, sep, sid = handle.partition(":")
    if not (sep and profile in homes):
        profile, sid = caller.profile, handle
    home = homes[profile]
    missing = ThreadError("thread_not_found", f"No chat {handle!r} of yours (ids from list or create; "
                          "another profile's thread is '<profile>:<id>').")
    with ts.reader(home) as conn:
        if conn is None or conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is None:
            raise missing
        lineage = ts.lineage(conn, sid)
        current = ts.tip(conn, sid)
        row = conn.execute("SELECT title, model, archived FROM sessions WHERE id=?", (current,)).fetchone()
        launch = ts.launch_of(conn, profile, lineage)
    if _subject(home, current) != caller.subject:
        raise missing
    return Thread(profile, home, current, lineage, (row["title"] or "") if row else "", row["model"] if row else None,
                  launch, bool(row["archived"]) if row else False)


def _require_own(caller: Caller, thread: Thread, verb: str) -> None:
    launch = thread.launch
    if not (launch and launch["launcher_profile"] == caller.profile and launch["launcher_session_id"] in caller.lineage):
        raise ThreadError("not_your_thread", (
            f"You can only {verb} threads this chat opened. A thread can't message its parent or other chats: "
            "it ends its turn with a clear final message, and the chat that opened it reads that."))


def _rows(thread: Thread) -> list[dict]:
    with ts.reader(thread.home) as conn:
        return ts.admissions(conn, thread.lineage) if conn is not None else []


# ── text the tool puts in front of a thread ───────────────────────────────────────────────────

def _header(caller: Caller) -> str:
    where = f"\u201c{caller.title}\u201d" if caller.title else caller.session_id
    return f"{MARK} From {caller.name} in {where} \u00b7 thread tool"


def _author(caller: Caller) -> dict:
    return {"id": f"bot:{caller.profile}", "name": caller.name, "is_bot": True}


def _brief_footer(caller: Caller) -> str:
    n = caller.name
    return (f"\n\n---\nYou're a thread {n} opened, a full chat with your own tools and skills. Work on this as you "
            f"would any task. If the brief is wrong or can't be done, say so and why; that's a good result. "
            f"When you're done, end your turn with a clear final message: the outcome, what you verified and how, "
            f"anything that needs the user, and open questions. {n} reads that message.\n"
            f"Authority: this brief comes from {n}, not from the user. It isn't the user's approval for spending "
            f"money, anything clients or members see, or sending to anyone but the user, so bring those back in your "
            f"final message instead. The user may open this thread and talk to you directly (their messages don't "
            f"start with {MARK}); if they do, they outrank {n}.")


def _clean(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThreadError("invalid_request", f"{name} is required.")
    value = value.strip()
    if len(value) > limit:
        raise ThreadError("invalid_request", f"{name} is too long ({len(value)} chars, max {limit}).")
    return value


def _key(value, *parts) -> str:
    if value is None:
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, default=str).encode()).hexdigest()[:24]
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ThreadError("invalid_request", "request_id must be 1-64 letters, digits, '-' or '_'.")
    return value


def _cap(text, limit):
    text = text or ""
    return (text, False) if len(text) <= limit else (text[:limit] + "\n…[truncated]", True)


def _seconds(value, name: str, default: float, top: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ThreadError("invalid_request", f"{name} must be a positive number of seconds.")
    return min(float(value), top)


# ── the owner connection ───────────────────────────────────────────────────────────────────────

def _run(coro):
    """Run a coroutine from a tool worker, off any running loop (the owner's own loop included)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _with_owner(home: Path, subject: str, fn):
    from hermes_cli.gateway_client import GatewayRPCError, connect_gateway
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    found = await asyncio.to_thread(discover_gateway_endpoint, home, timeout=5)
    if found.state != "ready" or found.endpoint is None:
        raise ThreadError("owner_unavailable", "The Hermes gateway that hosts these chats isn't running; try again shortly.")
    try:
        async with connect_gateway(endpoint=found.endpoint, subject=subject) as client:
            return await fn(client)
    except GatewayRPCError as exc:
        raise ThreadError("owner_refused", f"The gateway refused the request: {exc}") from exc


async def _attach(client, thread: Thread) -> dict:
    return await client.rpc("session.resume", session_id=thread.session_id, omit_messages=True, defer_history=True)


def _handed_back(caller: Caller, admission_ids) -> None:
    """What the tool itself stopped needs no thread update telling this chat it stopped."""
    for admission_id in admission_ids:
        ts.mark_delivered(caller.home, admission_id, "stopped")


# ── collaborators: the delegation menu and the teammates ──────────────────────────────────────

def _menu(cfg: dict) -> tuple[dict, str | None, dict]:
    backends = (cfg.get("delegation") or {}).get("backends") or {}
    model_map = backends.get("model_map") if isinstance(backends, dict) else None
    model_map = model_map if isinstance(model_map, dict) else {}
    options = {o["name"]: o for o in model_map.get("options") or [] if isinstance(o, dict) and o.get("name")}
    routes = backends.get("profile_routes") if isinstance(backends, dict) else None
    routes = routes if isinstance(routes, dict) else {}
    teammates = {p["name"]: p for p in routes.get("profiles") or [] if isinstance(p, dict) and p.get("name")}
    return options, model_map.get("default"), teammates


def _profile_model(home: Path) -> dict:
    from hermes_yaml import YAMLError
    from utils import load_yaml_file_readonly
    try:
        model = (load_yaml_file_readonly(home / "config.yaml") or {}).get("model")
    except (OSError, YAMLError):  # a teammate without a readable config just shows no model
        return {}
    if isinstance(model, dict):
        return {k: v for k, v in (("model", model.get("default")), ("provider", model.get("provider"))) if v}
    return {"model": model} if isinstance(model, str) else {}


def _collaborators(caller: Caller, args: dict) -> dict:
    options, default, teammates = _menu(_config())
    homes = ts.homes()
    return {
        "options": [{"option": name, "model": o.get("model"), "provider": o.get("provider"),
                     "reasoning": o.get("reasoning_effort"), "for": o.get("description")} for name, o in options.items()],
        "default_option": default,
        "note": ("create with no option runs on this chat's profile model; option picks one of these; model, provider "
                 "and reasoning override any of it. profile opens the thread as that teammate (their own memory, "
                 "skills and model)."),
        "teammates": [{"profile": name, "for": t.get("description"), **_profile_model(homes[name])}
                      for name, t in teammates.items() if name in homes and name != caller.profile],
        "this_profile": {"profile": caller.profile, **_profile_model(caller.home)},
    }


def _route(caller: Caller, args: dict) -> dict:
    options, _, teammates = _menu(_config())
    route: dict = {"profile": caller.profile}
    option = args.get("option")
    if option is not None:
        if option not in options:
            raise ThreadError("invalid_request", f"option must be one of: {', '.join(options) or '(none configured)'}.")
        o = options[option]
        route.update({k: o[src] for k, src in (("model", "model"), ("provider", "provider"),
                                               ("reasoning", "reasoning_effort"), ("service_tier", "service_tier"))
                      if isinstance(o.get(src), str) and o[src]})
        route["option"] = option
    for key in ("model", "provider", "reasoning"):
        value = args.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ThreadError("invalid_request", f"{key} must be a non-empty string.")
            route[key] = value.strip()
    if route.get("reasoning") is not None and route["reasoning"] not in REASONING:
        raise ThreadError("invalid_request", f"reasoning must be one of: {', '.join(REASONING)}.")
    profile = args.get("profile")
    if profile is not None and profile != caller.profile:
        if profile not in teammates or profile not in ts.homes():
            raise ThreadError("invalid_request", "profile must be one of the teammates: "
                              f"{', '.join(n for n in teammates if n != caller.profile) or '(none configured)'}.")
        route["profile"] = profile
    return route


# ── guards ─────────────────────────────────────────────────────────────────────────────────────

def _active(conn, launch_rows) -> int:
    return sum(1 for row in launch_rows
               if ts.thread_status(ts.admissions(conn, ts.lineage(conn, row["child_session_id"]))) in ACTIVE)


def _check_caps(caller: Caller) -> None:
    now = time.time()
    own = ts.threads_of(caller.profile, caller.lineage)
    recent = sum(1 for row in own if (row["created_at"] or 0) >= now - 3600)
    if recent >= MAX_CREATES_PER_HOUR:
        raise ThreadError("rate_limit", f"This chat opened {recent} threads in the last hour (max {MAX_CREATES_PER_HOUR}).")
    mine = box = 0
    for profile, home in ts.homes().items():
        with ts.reader(home) as conn:
            if conn is None:
                continue
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT child_session_id, launcher_profile, launcher_session_id FROM work_launches "
                    f"WHERE substr(launch_id,1,{len(ts.LAUNCH_PREFIX)})=? AND child_profile=? "
                    "AND child_session_id IS NOT NULL AND created_at>=?", (ts.LAUNCH_PREFIX, profile, now - 2 * 86400))]
                box += _active(conn, rows)
                mine += _active(conn, [r for r in rows if r["launcher_profile"] == caller.profile
                                       and r["launcher_session_id"] in caller.lineage])
            except sqlite3.Error:  # a store from before the launch ledger runs no threads
                continue
    if mine >= MAX_ACTIVE_PER_CHAT:
        raise ThreadError("fanout_limit", f"{mine} of this chat's threads are still working (max {MAX_ACTIVE_PER_CHAT}). "
                          "Wait on one, settle or stop one first.")
    if box >= MAX_ACTIVE_BOX:
        raise ThreadError("fanout_limit", f"{box} threads are working across the box (max {MAX_ACTIVE_BOX}). "
                          "Wait for some to finish first.")


def _toolset_ceiling(caller: Caller, profile: str):
    """A same-profile thread never gets more tools than its parent: pin the parent's set when the
    profile default would be broader. Teammates run with their own profile's tools."""
    if profile != caller.profile or caller.toolsets is None:
        return None
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import CLIENT_SURFACE_TOOLSETS
    if set(_get_platform_tools(_config(), "cli")) <= caller.toolsets | {"threads"}:
        return None
    # A client adds its own surface toolsets (desktop_ui, ...) to its sessions; create refuses them elsewhere.
    return sorted(caller.toolsets - {"threads"} - CLIENT_SURFACE_TOOLSETS)


@contextlib.contextmanager
def _create_lock():
    """Box-wide: chats in the dashboard, the gateway and ``hermes chat`` all create threads. Held across
    the cap check and the owner calls on purpose, so two creates can't both pass the same cap; parallel
    creates serialize (each is a few seconds), and the owner's own timeouts bound the wait."""
    path = ts.homes()["default"] / "cache" / "threads" / "create.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CREATE_LOCK, open(path, "ab") as fh:
        try:
            import fcntl
        except ImportError:  # no flock (Windows): the in-process lock still serializes this process
            yield
            return
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _check_unattended(caller: Caller) -> None:
    """The loop guard: what this chat sent into its threads since the user last wrote in it."""
    ids = list(caller.lineage)
    automatic = " ".join("AND substr(content,1,?)<>?" for _ in AUTOMATIC_PREFIXES)
    with ts.reader(caller.home) as conn:
        row = conn.execute(
            f"SELECT MAX(timestamp) FROM messages WHERE session_id IN ({','.join('?' * len(ids))}) AND role='user' "
            f"AND COALESCE(display_kind,'') IN ('','steer') AND COALESCE(display_metadata,'') NOT LIKE ? {automatic}",
            [*ids, f'%"{AUTOMATIC_KEY}"%', *(x for p in AUTOMATIC_PREFIXES for x in (len(p), p))]).fetchone()
    since = (row[0] if row else None) or 0
    homes, sent = ts.homes(), 0
    for launch in ts.threads_of(caller.profile, caller.lineage):
        with ts.reader(homes.get(launch["child_profile"], caller.home)) as conn:
            if conn is None:
                continue
            tids = list(ts.lineage(conn, launch["child_session_id"]))
            marks = ",".join("?" * len(tids))
            prefix = f"{MARK} From "
            sent += conn.execute(f"SELECT COUNT(*) FROM messages WHERE session_id IN ({marks}) AND role='user' "
                                 "AND timestamp>? AND substr(content,1,?)=?",
                                 [*tids, since, len(prefix), prefix]).fetchone()[0]
            sent += sum(1 for a in ts.admissions(conn, tids) if a["status"] == "queued" and ts.tool_sent(a))
    if sent >= MAX_SENDS_SINCE_USER:
        raise ThreadError("check_in_with_user", (
            f"This chat has sent {sent} messages into its threads since the user last wrote here (max "
            f"{MAX_SENDS_SINCE_USER}). Tell the user where things stand and what you'd do next; their next "
            "message here resets this."))


def _turn_mark(caller: Caller) -> int:
    """The caller's current turn: its newest user-role row (a typed prompt or a wake). A send repeated
    within one turn is the same send; the same words in a later turn are a new one."""
    ids = list(caller.lineage)
    with ts.reader(caller.home) as conn:
        row = conn.execute(f"SELECT MAX(id) FROM messages WHERE session_id IN ({','.join('?' * len(ids))}) "
                           "AND role='user'", ids).fetchone()
    return (row[0] if row else None) or 0


async def _clear_paused(client, thread: Thread, rows: list[dict], force: bool) -> list[str]:
    """Resolve the ``unknown`` turns a restart left at the head of the thread's queue (without this
    nothing behind them runs). Only the tool's own, unless ``force``: the user's stay theirs."""
    cleared = []
    for row in rows:
        if row["status"] != "unknown":
            continue
        if not (ts.tool_sent(row) or force):
            raise ThreadError("thread_paused", (
                "A restart cut off the user's own turn in that thread, and its queue waits on it. Ask the user to "
                "open the thread and discard it, or pass force=true to discard it yourself."))
        await client.rpc("prompt.resolve_unknown", session_id=thread.session_id, admission_id=row["admission_id"],
                         execution_generation=row["generation"])
        cleared.append(row["admission_id"])
    return cleared


# ── actions ────────────────────────────────────────────────────────────────────────────────────

def _wake_hint(caller: Caller) -> str:
    """How the parent hears back: the dashboard's per-chat poller wakes chats it hosts."""
    server = sys.modules.get("tui_gateway.server")
    sessions = getattr(server, "_sessions", None) or {}
    hosted = any(str(s.get("session_key") or "") in caller.lineage for s in list(sessions.values()) if isinstance(s, dict))
    if hosted:
        return ("When it finishes, this chat gets a thread update with the result, so you can end your turn or do "
                "other work; use wait only when you need the result now.")
    return "This chat isn't woken by thread updates: use wait (repeat it while threads work) or list to check back."


def _create(caller: Caller, args: dict) -> dict:
    if caller.launch is not None:
        raise ThreadError("depth_limit", "Threads can't open threads. Use delegate_task for subtasks, or say in your "
                          "final message what more work you'd open, and the chat that opened you can.")
    title = _clean(args.get("title"), "title", TITLE_MAX)
    brief = _clean(args.get("brief"), "brief", TEXT_MAX)
    route = _route(caller, args)
    profile = route["profile"]
    home = ts.homes()[profile]
    cwd = args.get("cwd")
    if cwd is not None and not (isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)):
        raise ThreadError("invalid_request", "cwd must be an existing absolute directory.")
    cwd = cwd or caller.cwd
    budget = args.get("run_budget_s")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0):
        raise ThreadError("invalid_request", "run_budget_s must be 0 (the profile's default) or a number of seconds.")
    budget = RUN_BUDGET_DEFAULT_S if budget is None else budget
    key = _key(args.get("request_id"), caller.session_id, title, brief, sorted(route.items()), cwd, budget,
               _turn_mark(caller))
    create_id, launch_id = ts.REQUEST_PREFIX + key, ts.LAUNCH_PREFIX + key
    display = f"{MARK} {title}"
    text = f"{_header(caller)}\n\n{brief}{_brief_footer(caller)}"
    params: dict = dict(request_id=create_id, source="tui", started_by_session=caller.session_id,
                  started_by_profile=caller.profile, launch_kind="lane" if profile == caller.profile else "teammate",
                  launch_title=title, launch_id=launch_id)
    params.update({k: route[k] for k in ("model", "provider", "reasoning", "service_tier") if k in route})
    if cwd:
        params["cwd"] = cwd
    if budget:
        params["run_budget"] = float(min(max(budget, RUN_BUDGET_MIN_S), RUN_BUDGET_MAX_S))
    ceiling = _toolset_ceiling(caller, profile)
    if ceiling is not None:
        params["toolsets"] = ceiling

    async def go(client):
        # Untitled on purpose: a titled session.create attaches to any existing chat with that title.
        snap = await client.rpc("session.create", **params)
        sid = snap["session_id"]
        bound = await asyncio.to_thread(_bound_child, home, launch_id)
        if bound not in (sid, snap.get("stored_session_id")):
            raise ThreadError("lineage_missing", f"Opened {sid} but its launch record is missing, so the brief wasn't "
                              "sent. Nothing is running there.")
        titled = snap["info"].get("title") or ""
        if not titled.startswith(display):
            titled = await _rename(client, sid, [display, f"{display} \u00b7 {sid[-4:]}"], create_id + ":title")
        admission = await client.rpc("prompt.submit", session_id=sid, submission_id=create_id + ":brief", text=text,
                                     finite=True, turn_author=_author(caller))
        return sid, titled, admission, snap["info"].get("model")

    with _create_lock():
        with ts.reader(home) as conn:
            reused = conn is not None and conn.execute(
                "SELECT 1 FROM work_launches WHERE launch_id=?", (launch_id,)).fetchone() is not None
        if not reused:  # an exact retry of an earlier create always finishes
            _check_caps(caller)
            _check_unattended(caller)
        sid, titled, admission, model = _run(_with_owner(home, caller.subject, go))
    handle = _handle(caller, profile, sid)
    status = "queued" if admission.get("status") == "queued" else "running"
    if reused:  # the replayed brief may have finished long ago: say how it stands
        with ts.reader(home) as conn:
            row = next((r for r in ts.admissions(conn, ts.lineage(conn, sid))
                        if r["admission_id"] == admission["admission_id"]), None)
        status = ts.admission_status(row) if row is not None else status
    out = {"thread_id": handle, "title": titled, "admission_id": admission["admission_id"], "status": status,
           "profile": profile, "model": model, "request_id": key}
    if "option" in route:
        out["option"] = route["option"]
    if reused:
        out["reused"] = True
        out["note"] = ("Same title, brief and route as a create earlier in this turn: returned that thread. Pass a "
                       "new request_id to open another.")
    if args.get("wait_s"):
        out["wait"] = _wait(caller, {"thread_id": handle, "admission_id": out["admission_id"], "timeout_s": args["wait_s"]})
    else:
        out["next"] = _wake_hint(caller)
    return out


def _bound_child(home: Path, launch_id: str):
    with ts.reader(home) as conn:
        row = conn and conn.execute("SELECT child_session_id FROM work_launches WHERE launch_id=?", (launch_id,)).fetchone()
        return row[0] if row else None


async def _rename(client, sid, titles, request_id):
    from hermes_cli.gateway_client import GatewayRPCError
    for attempt, title in enumerate(titles):
        # Each attempt reads the revision afresh: a refused first title must not fence out the second.
        snap = await client.rpc("session.resume", session_id=sid, omit_messages=True, defer_history=True)
        try:
            await client.rpc("session.mutate", session_id=sid, request_id=f"{request_id}:{attempt}",
                             expected_revision=snap["revision"], operation="rename", payload={"title": title[:100]})
            return title[:100]
        except GatewayRPCError:
            continue  # titles are unique per profile: try the next one
    return ""


def _send(caller: Caller, args: dict) -> dict:
    message = _clean(args.get("message"), "message", TEXT_MAX)
    mode = args.get("mode") or "queue"
    if mode not in {"queue", "interrupt"}:
        raise ThreadError("invalid_request", "mode must be 'queue' or 'interrupt'.")
    force = args.get("force") is True
    thread = _resolve(caller, args.get("thread_id"))
    _require_own(caller, thread, "message")
    rows = _rows(thread)
    key = _key(args.get("request_id"), caller.session_id, thread.session_id, message, _turn_mark(caller))
    submission = f"{ts.REQUEST_PREFIX}send-{key}"
    earlier = next((row for row in rows if row["request_id"] == submission), None)
    if earlier is not None:
        # An exact retry (a lost reply, or the same send twice in one turn) replays the first one and
        # acts on nothing: re-running an interrupt here would cancel the very message it queued.
        return {"thread_id": thread.handle(caller), "title": thread.title, "status": ts.admission_status(earlier),
                "admission_id": earlier["admission_id"], "request_id": key, "reused": True,
                "note": "Already sent (same message in this turn): returned that send."}
    _check_unattended(caller)
    text = f"{_header(caller)}\n\n{message}"
    running = next((row for row in rows if row["status"] == "started"), None)
    queued = [row for row in rows if row["status"] == "queued" and ts.tool_sent(row)]

    async def go(client):
        snap = await _attach(client, thread)
        notes = []
        if await _clear_paused(client, thread, rows, force):
            notes.append("Cleared a turn a gateway restart had cut off, so the thread's queue moves again.")
        if mode == "interrupt" and snap.get("running") and running is not None:
            if ts.tool_sent(running) or force:
                for row in queued:
                    await client.rpc("prompt.cancel", session_id=thread.session_id, admission_id=row["admission_id"])
                await client.rpc("session.interrupt", session_id=thread.session_id,
                                 execution_generation=snap["execution_generation"])
                _handed_back(caller, [row["admission_id"] for row in [*queued, running] if ts.tool_sent(row)])
                notes.append("Interrupted the turn you sent (and dropped what you had queued); this message runs "
                             "next, with the thread's history intact.")
            else:
                notes.append("The user's own turn is running there, so this was queued after it (force=true "
                             "interrupts the user's turn).")
        admission = await client.rpc("prompt.submit", session_id=thread.session_id, submission_id=submission,
                                     text=text, finite=True, turn_author=_author(caller))
        busy = snap.get("running") and not any(n.startswith("Interrupted") for n in notes)
        if busy and not notes:
            notes.append("The thread was busy, so this runs after its current turn.")
        out = {"status": "queued" if snap.get("running") else "running", "admission_id": admission["admission_id"]}
        if notes:
            out["note"] = " ".join(notes)
        return out

    out = {"thread_id": thread.handle(caller), "title": thread.title, **_run(_with_owner(thread.home, caller.subject, go)),
           "request_id": key}
    if args.get("wait_s"):
        out["wait"] = _wait(caller, {"thread_id": out["thread_id"], "admission_id": out["admission_id"],
                                     "timeout_s": args["wait_s"]})
    return out


def _report(caller: Caller, thread: Thread, admission: dict | None, cap: int) -> dict:
    """One thread's state for the caller, with the pinned turn's result once it settled."""
    with ts.reader(thread.home) as conn:
        rows = ts.admissions(conn, thread.lineage) if conn is not None else []
        if admission is not None:
            admission = next((row for row in rows if row["admission_id"] == admission["admission_id"]), admission)
        status = ts.admission_status(admission)
        out = {"thread_id": thread.handle(caller), "title": thread.title, "status": status,
               "thread_status": ts.thread_status(rows)}
        if admission is not None:
            out["admission_id"] = admission["admission_id"]
            if not ts.tool_sent(admission):
                out["turn_by"] = "user"
        if status in {"done", "failed", "stopped"} and admission is not None and conn is not None:
            result = ts.outcome(conn, admission, thread.lineage)
            out["final_response"], truncated = _cap(result["final_response"], cap)
            if truncated:
                out["truncated"] = "use read with full=true for the whole message"
            if result.get("error") and status != "done":
                out["error"] = str(result["error"])[:500]
            if result.get("usage"):
                out["usage"] = result["usage"]
        elif status == "paused":
            out["note"] = ("A gateway restart cut this turn off, and the thread's queue waits on it. stop clears it "
                           "(it doesn't rerun); then send to carry on.")
    return out


def _mark_delivered(caller: Caller, thread: Thread, report: dict) -> None:
    if (thread.launch and thread.launch["launcher_session_id"] in caller.lineage
            and report.get("admission_id") and report["status"] not in {"queued", "running", "idle"}):
        ts.mark_delivered(caller.home, report["admission_id"], report["status"])


def _wait(caller: Caller, args: dict) -> dict:
    handles = args.get("thread_ids") or ([args["thread_id"]] if args.get("thread_id") else [])
    if not isinstance(handles, list) or not handles or len(handles) > WAIT_MANY_MAX:
        raise ThreadError("invalid_request", f"Give thread_id, or thread_ids (a list of at most {WAIT_MANY_MAX}).")
    timeout = _seconds(args.get("timeout_s"), "timeout_s", WAIT_DEFAULT_S, WAIT_MAX_S)
    first = args.get("return_when") == "first"
    threads = [_resolve(caller, handle) for handle in dict.fromkeys(handles)]
    named = args.get("admission_id") if len(threads) == 1 else None
    pins = {}
    for thread in threads:
        pin = ts.pinned(_rows(thread), named)
        if named and pin is None:
            raise ThreadError("invalid_request", f"admission {named!r} isn't a turn of that thread.")
        pins[thread.session_id] = pin
    cap = FINAL_CAP if len(threads) == 1 else FINAL_CAP_MANY
    started = time.monotonic()
    deadline = started + timeout
    while True:
        reports = [_report(caller, thread, pins[thread.session_id], cap) for thread in threads]
        settled = [r for r in reports if r["status"] not in {"queued", "running"}]
        note = None if (first and settled) or len(settled) == len(reports) else _wait_ended(caller, deadline)
        if note == "":
            time.sleep(POLL_S)
            continue
        for thread, report in zip(threads, reports):
            _mark_delivered(caller, thread, report)
        out = {"threads": reports, "waited_s": round(time.monotonic() - started, 1)}
        if note:
            out["note"] = note
        return out


def _wait_ended(caller: Caller, deadline: float) -> str:
    """Why an unsettled wait ends now, or "" to keep polling."""
    from tools.interrupt import consume_yield, is_interrupted
    if is_interrupted():
        return "Interrupted: the wait ended, and the threads keep working."
    if consume_yield(threading.current_thread().ident):
        return ("The user sent a new message, so the wait ended and the threads keep working. Respond to the "
                "user now; their results will come back to you.")
    if time.monotonic() >= deadline:
        return "Timed out: the threads keep working. " + _wake_hint(caller)
    return ""


def _author_of(role: str, text: str) -> str:
    if role == "assistant":
        return "assistant"
    if text.startswith(f"{MARK} From "):
        return "agent"
    if text.startswith("[Thread update"):
        return "thread_update"
    return "automatic" if text.startswith(AUTOMATIC_PREFIXES) else "user"


def _read(caller: Caller, args: dict) -> dict:
    limit = args.get("limit") or READ_DEFAULT
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ThreadError("invalid_request", "limit must be a positive integer.")
    limit = min(limit, READ_MAX)
    after = args.get("after")
    cap = MESSAGE_CAP_FULL if args.get("full") is True else MESSAGE_CAP
    thread = _resolve(caller, args.get("thread_id"))
    ids = list(thread.lineage)
    with ts.reader(thread.home) as conn:
        if conn is None:
            raise ThreadError("thread_not_found", "That chat's profile has no session store.")
        base = (f"SELECT id, role, content, timestamp FROM messages WHERE session_id IN ({','.join('?' * len(ids))}) "
                "AND role IN ('user','assistant') AND content IS NOT NULL AND content<>''")
        if isinstance(after, int) and not isinstance(after, bool):
            rows = conn.execute(base + " AND id>? ORDER BY id LIMIT ?", [*ids, after, limit]).fetchall()
        else:
            rows = list(reversed(conn.execute(base + " ORDER BY id DESC LIMIT ?", [*ids, limit]).fetchall()))
        history = ts.admissions(conn, thread.lineage)
    messages = []
    for r in rows:
        text, truncated = _cap(r["content"], cap)
        item = {"id": r["id"], "author": _author_of(r["role"], r["content"]), "text": text, "at": r["timestamp"]}
        if truncated:
            item["truncated"] = True
        messages.append(item)
    out = {"thread_id": thread.handle(caller), "title": thread.title, "status": ts.thread_status(history),
           "messages": messages}
    if messages:
        out["next_after"] = messages[-1]["id"]
    if after is None:  # the tail is in hand: its settled results needn't wake this chat again
        for admission in history:
            if ts.tool_sent(admission):
                _mark_delivered(caller, thread, {"admission_id": admission["admission_id"],
                                                 "status": ts.admission_status(admission)})
    return out


def _list(caller: Caller, args: dict) -> dict:
    include = args.get("include_settled") is True
    out, seen = [], set()
    homes = ts.homes()
    for launch in ts.threads_of(caller.profile, caller.lineage):
        home = homes.get(launch["child_profile"])
        if home is None:
            continue
        with ts.reader(home) as conn:
            if conn is None:
                continue
            ids = ts.lineage(conn, launch["child_session_id"])
            if ids & seen:
                continue
            seen |= ids
            current = ts.tip(conn, launch["child_session_id"])
            info = conn.execute("SELECT title, model, archived, last_activity_at FROM sessions WHERE id=?",
                                (current,)).fetchone()
            if info is None or (info["archived"] and not include):
                continue
            preview, _ = _cap(ts.last_reply(conn, ids), PREVIEW_CAP)
            profile = launch["child_profile"]
            out.append({"thread_id": _handle(caller, profile, current),
                        "title": info["title"], "status": ts.thread_status(ts.admissions(conn, ids)),
                        "profile": profile, "model": info["model"], "settled": bool(info["archived"]),
                        "last_activity_at": info["last_activity_at"], "preview": preview})
    return {"threads": out}


def _stop(caller: Caller, args: dict) -> dict:
    thread = _resolve(caller, args.get("thread_id"))
    _require_own(caller, thread, "stop")
    force = args.get("force") is True
    rows = _rows(thread)
    queued = [row for row in rows if row["status"] == "queued" and ts.tool_sent(row)]
    running = next((row for row in rows if row["status"] == "started"), None)

    async def go(client):
        snap = await _attach(client, thread)
        cleared = await _clear_paused(client, thread, rows, force)
        cancelled, cancelled_running = [], []
        for row in queued:
            receipt = await client.rpc("prompt.cancel", session_id=thread.session_id, admission_id=row["admission_id"])
            if receipt.get("status") == "terminal":
                cancelled.append(row["admission_id"])
        _handed_back(caller, cancelled)
        out: dict = {"cancelled_queued": cancelled}
        if cleared:
            out["cleared_paused"] = cleared
        if snap.get("running") and running is not None:
            if ts.tool_sent(running) or force:
                await client.rpc("session.interrupt", session_id=thread.session_id,
                                 execution_generation=snap["execution_generation"])
                if ts.tool_sent(running):
                    cancelled_running = [running["admission_id"]]
                out["status"] = "stopping"
            else:
                out["status"] = "running"
                out["note"] = "The user's own turn is running there, so it was left alone (force=true stops it)."
        else:
            out["status"] = "idle"
            if not (cancelled or cleared):
                out["note"] = "Nothing was running or queued."
        _handed_back(caller, cancelled_running)
        return out

    return {"thread_id": thread.handle(caller), "title": thread.title,
            **_run(_with_owner(thread.home, caller.subject, go))}


def _mutate(caller: Caller, thread: Thread, operation: str, payload: dict, request_id: str) -> dict:
    async def go(client):
        snap = await _attach(client, thread)
        return await client.rpc("session.mutate", session_id=thread.session_id, request_id=request_id,
                                expected_revision=snap["revision"], operation=operation, payload=payload)
    return _run(_with_owner(thread.home, caller.subject, go))


def _settle(caller: Caller, args: dict) -> dict:
    thread = _resolve(caller, args.get("thread_id"))
    _require_own(caller, thread, "settle")
    status = ts.thread_status(_rows(thread))
    if status in {"running", "queued"}:
        raise ThreadError("thread_busy", "That thread is still working: wait for it, or stop it first.")
    if not thread.archived:
        _mutate(caller, thread, "archive", {"archived": True}, f"{ts.REQUEST_PREFIX}settle-{_key(None, thread.session_id)}")
    return {"thread_id": thread.handle(caller), "title": thread.title, "status": status, "settled": True,
            "note": "Archived: out of the chat list, still searchable and readable. Family keeps it until it ages out."}


def _rename_action(caller: Caller, args: dict) -> dict:
    thread = _resolve(caller, args.get("thread_id"))
    _require_own(caller, thread, "rename")
    title = f"{MARK} {_clean(args.get('title'), 'title', TITLE_MAX)}"
    _mutate(caller, thread, "rename", {"title": title}, f"{ts.REQUEST_PREFIX}rename-{_key(None, thread.session_id, title)}")
    return {"thread_id": thread.handle(caller), "title": title}


ACTIONS = {"create": _create, "send": _send, "wait": _wait, "read": _read, "list": _list, "stop": _stop,
           "settle": _settle, "rename": _rename_action, "collaborators": _collaborators}


def thread_tool(args: dict, parent_agent=None) -> str:
    action = (args or {}).get("action")
    try:
        if action not in ACTIONS:
            raise ThreadError("invalid_request", f"action must be one of: {', '.join(ACTIONS)}.")
        result = ACTIONS[action](_caller(parent_agent), args)
        if action in {"create", "send"} and parent_agent is not None:
            # The dashboard's wake poller scans an idle chat once a minute; new work here rescans it at once.
            with contextlib.suppress(AttributeError):
                parent_agent._thread_launched = True
        return json.dumps({"success": True, "action": action, **result}, ensure_ascii=False)
    except ThreadError as exc:
        return json.dumps({"success": False, "action": action, "error": exc.code, "message": str(exc)},
                          ensure_ascii=False)
    except sqlite3.Error as exc:
        return json.dumps({"success": False, "action": action, "error": "store_unavailable",
                           "message": f"A session store couldn't be read ({exc}); try again shortly."},
                          ensure_ascii=False)


def _p(kind, description, **extra):
    return {"type": kind, "description": description, **extra}


THREAD_SCHEMA = {
    "name": "thread",
    "description": (
        "Lead parallel work through threads: visible chats of the user's that work while you lead. A thread is a "
        "full agent chat with its own tools and skills; it shows in the user's chat list (titled with "
        f"{MARK}) and under this chat in Family, and the user can open it and talk to it at any time.\n"
        "Actions: create (open a thread with a self-contained brief; it sees nothing of this chat). send (message "
        "a thread you opened: mode queue runs it after the current turn; interrupt stops the turn you sent there "
        "and runs this next). "
        "wait (until the threads' turns finish, at most "
        f"{WAIT_MAX_S}s; a timeout never stops the work). read (page any of the user's local chats). list (this "
        "chat's threads). stop (cancel what you queued and interrupt your running turn there). settle (archive a "
        "finished thread). rename. collaborators (the models, reasoning levels and teammate profiles create can "
        "use).\n"
        "When to use: threads for work worth seeing or joining, that runs a while, or that runs in parallel (2-4 "
        "for comparisons, parallel research or independent reviews; one writer per checkout). delegate_task for "
        "quick isolated subtasks. Doing it yourself for small or sequential work.\n"
        "A good brief: the goal and why it matters, what done looks like and how it's checked, context and paths, "
        "constraints and what the thread owns, and what to report. Say why, invite pushback, and verify what comes "
        "back. A thread can't message its parent: it ends its turn with a clear final message, and when this chat "
        "can be woken, that result comes back to you as a thread update. Everything you send is visibly marked as "
        "from you, and threads can't open threads."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": _p("string", "What to do.", enum=list(ACTIONS)),
            "title": _p("string", f"create, rename: a short title (max {TITLE_MAX} chars), shown with a {MARK} prefix."),
            "brief": _p("string", "create: everything the thread needs. It knows nothing else."),
            "option": _p("string", "create: a collaborator option from collaborators (a model, provider and reasoning "
                         "preset)."),
            "profile": _p("string", "create: open it as a teammate profile from collaborators."),
            "model": _p("string", "create: a model id (overrides option and the profile default)."),
            "provider": _p("string", "create: the provider for model."),
            "reasoning": _p("string", "create: reasoning effort.", enum=REASONING),
            "cwd": _p("string", "create: absolute working directory (default: this chat's)."),
            "run_budget_s": _p("integer", f"create: wall-clock seconds per turn in that chat, the user's turns there "
                               f"included (default {RUN_BUDGET_DEFAULT_S}, max {RUN_BUDGET_MAX_S}; 0 for the profile "
                               "default)."),
            "thread_id": _p("string", "The thread: its id from create or list ('<profile>:<id>' for a teammate's)."),
            "thread_ids": {"type": "array", "items": {"type": "string"}, "description": "wait: several threads."},
            "message": _p("string", "send: the message. When changing course, restate the goal and why, not just "
                          "the change."),
            "mode": _p("string", "send: queue (default) or interrupt.", enum=["queue", "interrupt"]),
            "force": _p("boolean", "send, stop: also act on the user's own turn there (interrupt it, or discard it "
                        "when a restart cut it off)."),
            "admission_id": _p("string", "wait: the turn to wait on (default: the newest one you sent)."),
            "wait_s": _p("integer", f"create, send: also wait up to this many seconds (max {WAIT_MAX_S})."),
            "timeout_s": _p("integer", f"wait: max seconds (default {WAIT_DEFAULT_S}, max {WAIT_MAX_S})."),
            "return_when": _p("string", "wait: all (default) or first.", enum=["all", "first"]),
            "after": _p("integer", "read: messages after this id (from next_after)."),
            "limit": _p("integer", f"read: how many messages (default {READ_DEFAULT}, max {READ_MAX})."),
            "full": _p("boolean", f"read: up to {MESSAGE_CAP_FULL} chars per message instead of {MESSAGE_CAP}."),
            "include_settled": _p("boolean", "list: include settled (archived) threads."),
            "request_id": _p("string", "create, send: idempotency key. A repeat with the same key (or, by default, the "
                             "same content) returns the same work instead of doing it twice."),
        },
        "required": ["action"],
    },
}


def _registry_handler(args, **kw):
    """The agent loop runs ``thread`` inline with its agent (``agent/inline_tool_executors.py``). Any
    other dispatch route gets only the dispatcher's ``session_id``: that still names the calling chat,
    so the same login-principal and ownership guards apply (without the parent's toolset ceiling)."""
    sid = kw.get("session_id")
    return thread_tool(args, parent_agent=SimpleNamespace(session_id=sid) if isinstance(sid, str) and sid else None)


registry.register(
    name="thread",
    toolset="threads",
    schema=THREAD_SCHEMA,
    handler=_registry_handler,
    emoji="\U0001f9f5",
)
