"""Authenticated registrations and canonical native event projection, never approval execution."""

import re

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_MOBILE_PUSH_METHODS = (
    "mobile.push.status", "mobile.push.register", "mobile.push.unregister",
    "mobile.activity.register", "mobile.activity.unregister",
    "mobile.push.refresh", "mobile.activity.refresh",
    "mobile.session_push.register", "mobile.session_push.refresh", "mobile.session_push.unregister",
    "mobile.session_activity.register", "mobile.session_activity.refresh", "mobile.session_activity.unregister",
    "mobile.widget.register", "mobile.widget.inbox.register", "mobile.widget.unregister", "mobile.session_push.presence",
    "mobile.session_push.policy", "mobile.session_activity.start_token.register",
    "mobile.session_activity.start_token.unregister",
)
_MOBILE_PUSH_STATUSES = {
    "tool.start": "usingTool", "tool.complete": "thinking", "message.delta": "responding",
    "thinking.delta": "thinking", "approval.request": "waitingForApproval",
    "clarify.request": "waitingForClarification",
}
_MOBILE_PUSH_EVENTS = frozenset(_MOBILE_PUSH_STATUSES) | {
    "message.start", "message.complete", "request.cancel", "todo.updated"}
# Each accepted human prompt that will run as its own turn marks one message.start as human;
# continuations never pass prompt.submit. Markers older than this belong to turns that never ran.
_MOBILE_PUSH_HUMAN_MARKER_TTL = 3 * 3600
_EDIT_TOOLS = frozenset({"write_file", "patch", "apply_patch", "edit_file", "str_replace_editor"})
_TEST_COMMAND = re.compile(r"\b(pytest|run_tests|unittest|vitest|jest|xcodebuild\s+test|go\s+test|cargo\s+test|npm\s+(?:run\s+)?test)\b")
_STEP_LABELS = {
    "read_file": "Reading", "search_files": "Searching files", "execute_code": "Running code",
    "web_search": "Searching the web", "web_extract": "Reading the web", "delegate_task": "Delegating",
    "todo": "Updating the plan", "skill_view": "Reading a skill", "session_search": "Searching past chats",
    "vision_analyze": "Looking at an image", "image_generate": "Making an image",
}


def _mobile_push_service():
    from tui_gateway.mobile_push import service_for_home
    if os.environ.get("HERMES_COMPUTE_HOST_CHILD") == "1":
        return None  # the supervisor projects the relayed events once
    return service_for_home(_hermes_home)


def _mobile_push_handler(name):
    def decorate(fn):
        def handle(rid, params):
            import sqlite3
            from tui_gateway.methods_browser_control import _is_authenticated_identity, _principal_digest
            identity = getattr(current_transport(), "auth_identity", None)
            if not _is_authenticated_identity(identity):
                return _err(rid, 4403, "authenticated mobile identity required")
            try:
                return fn(rid, params, _principal_digest(identity))
            except (ValueError, FileNotFoundError, TypeError):
                return _err(rid, 4400, "invalid mobile notification scope or registration")
            except (sqlite3.Error, RuntimeError, OSError):
                return _err(rid, 4401, "mobile notifications temporarily unavailable")
        return method(name)(handle)
    return decorate


@_mobile_push_handler("mobile.push.status")
def _(rid, params, principal):
    return _ok(rid, {"available": _mobile_push_service() is not None, "protocol_version": 1})


def _mobile_push_register(rid, params, principal, kind):
    from tui_gateway.mobile_push import Scope
    service = _mobile_push_service()
    if service is None:
        return _err(rid, 4405, "mobile notifications are not configured")
    profile, _home, _sid, session, root, *_rest = _mobile_scope(params)
    push_scope = Scope("native", profile, root["id"])
    options = {"installation_id": params.get("installation_id"), "connection_id": params.get("connection_id"),
        "token": params.get("activity_token" if kind == "activity" else "device_token"),
        "environment": params.get("environment"), "kind": kind}
    if kind == "activity":
        options.update(activity_id=params.get("activity_id"), run_id=params.get("run_id"))
    else:
        options["categories"] = params.get("categories", ("attention", "completion"))
        options["preview_enabled"] = params.get("preview_enabled", False)
    receipt = service.register(principal, push_scope, **options)
    session["_mobile_push_scope"] = push_scope
    return _ok(rid, receipt)


@_mobile_push_handler("mobile.push.register")
def _(rid, params, principal):
    return _mobile_push_register(rid, params, principal, "alert")


@_mobile_push_handler("mobile.activity.register")
def _(rid, params, principal):
    return _mobile_push_register(rid, params, principal, "activity")


def _mobile_push_refresh(rid, params, principal, kind):
    service = _mobile_push_service()
    if service is None:
        return _err(rid, 4405, "mobile notifications are not configured")

    def accepts_scope(scope):
        if scope.surface != "native":
            return False
        try:
            _profile, home = _mobile_profile({"profile": scope.profile})
            with _mobile_read_db(home) as db:
                _mobile_canonical_identity(db, scope.session_id)
            return True
        except (ValueError, FileNotFoundError):
            return False

    options = {"installation_id": params.get("installation_id"), "connection_id": params.get("connection_id"),
        "token": params.get("activity_token" if kind == "activity" else "device_token"),
        "environment": params.get("environment"), "kind": kind, "accepts_scope": accepts_scope}
    if kind == "activity":
        options.update(activity_id=params.get("activity_id"), run_id=params.get("run_id"))
    else:
        options["categories"] = params.get("categories", ("attention", "completion"))
        options["preview_enabled"] = params.get("preview_enabled", False)
    return _ok(rid, service.refresh(principal, **options))


@_mobile_push_handler("mobile.push.refresh")
def _(rid, params, principal):
    return _mobile_push_refresh(rid, params, principal, "alert")


@_mobile_push_handler("mobile.activity.refresh")
def _(rid, params, principal):
    return _mobile_push_refresh(rid, params, principal, "activity")


def _mobile_push_unregister(rid, params, principal, kind=None):
    from tui_gateway.mobile_push import unregister_for_home
    service = _mobile_push_service()
    options = dict(installation_id=params.get("installation_id"), connection_id=params.get("connection_id"),
                   subscription_id=params.get("subscription_id"), kind=kind)
    removed = (service.unregister(principal, **options) if service else
               unregister_for_home(_hermes_home, principal, **options))
    return _ok(rid, {"removed": removed})


@_mobile_push_handler("mobile.push.unregister")
def _(rid, params, principal):
    return _mobile_push_unregister(rid, params, principal)


@_mobile_push_handler("mobile.activity.unregister")
def _(rid, params, principal):
    if not params.get("subscription_id"):
        raise ValueError("subscription_id required")
    return _mobile_push_unregister(rid, params, principal, "activity")


def _mobile_session_destination(params):
    from tui_gateway.mobile_session_scope import resolve_destination
    profile, home = _mobile_profile(params)
    return resolve_destination(profile, home, _mobile_string(params, "stored_session_id"))


def _mobile_session_register(rid, params, principal, kind):
    service = _mobile_push_service()
    if service is None:
        return _err(rid, 4405, "mobile notifications are not configured")
    destination, tip, _title = _mobile_session_destination(params)
    token_key = {"alert": "device_token", "activity": "activity_token", "widget": "widget_token"}[kind]
    options = dict(installation_id=params.get("installation_id"), connection_id=params.get("connection_id"),
                   token=params.get(token_key, "" if kind == "widget" else None), environment=params.get("environment"), kind=kind)
    if kind == "activity":
        options.update(activity_id=params.get("activity_id"), run_id=params.get("run_id"))
    elif kind == "widget":
        options.update(read_token=params.get("read_token"))
    else:
        options.update(categories=params.get("categories", ("attention", "completion")),
                       preview_enabled=params.get("preview_enabled", False))
    receipt = service.register(principal, destination, **options)
    receipt.update(destination={"surface": destination.surface, "profile": destination.profile,
                                "session_id": destination.session_id}, resolved_session_id=tip,
                   notification_run=service.current_run(destination))
    if kind == "widget":
        from tui_gateway.mobile_widget_http import SNAPSHOT_PATH
        receipt["snapshot_path"] = SNAPSHOT_PATH
    return _ok(rid, receipt)


@_mobile_push_handler("mobile.session_push.register")
def _(rid, params, principal):
    return _mobile_session_register(rid, params, principal, "alert")


@_mobile_push_handler("mobile.session_activity.register")
def _(rid, params, principal):
    return _mobile_session_register(rid, params, principal, "activity")


@_mobile_push_handler("mobile.widget.register")
def _(rid, params, principal):
    return _mobile_session_register(rid, params, principal, "widget")


@_mobile_push_handler("mobile.widget.inbox.register")
def _(rid, params, principal):
    from tui_gateway.mobile_widget_inbox import profile_home
    service = _mobile_push_service()
    if service is None:
        return _err(rid, 4405, "mobile notifications are not configured")
    profile = _mobile_string(params, "profile")
    home = profile_home(profile)
    return _ok(rid, service.store.register_widget_inbox(principal,
        installation_id=params.get("installation_id"), connection_id=params.get("connection_id"),
        profile=profile, home=home, token=params.get("read_token")))


@_mobile_push_handler("mobile.session_push.presence")
def _(rid, params, principal):
    service = _mobile_push_service()
    if service is None:
        return _err(rid, 4405, "mobile notifications are not configured")
    destination, _tip, _title = _mobile_session_destination(params)
    return _ok(rid, service.store.presence(principal, destination,
        installation_id=params.get("installation_id"), connection_id=params.get("connection_id"),
        foreground=params.get("foreground")))


@_mobile_push_handler("mobile.session_push.policy")
def _(rid, params, principal):
    """Read (no ``policy``) or set this device's alert policy: "all" listed chats or "opened"."""
    service = _mobile_push_service()
    if service is None:
        return _err(rid, 4405, "mobile notifications are not configured")
    ids = dict(installation_id=params.get("installation_id"), connection_id=params.get("connection_id"))
    if params.get("policy") is None:
        return _ok(rid, service.store.policy(principal, **ids))
    return _ok(rid, service.set_policy(principal, **ids, policy=params.get("policy"),
        token=params.get("device_token"), environment=params.get("environment"),
        categories=params.get("categories", ("attention", "completion")),
        preview_enabled=params.get("preview_enabled", False)))


@_mobile_push_handler("mobile.session_activity.start_token.register")
def _(rid, params, principal):
    service = _mobile_push_service()
    if service is None:
        return _err(rid, 4405, "mobile notifications are not configured")
    return _ok(rid, service.register_start_token(principal, installation_id=params.get("installation_id"),
        connection_id=params.get("connection_id"), token=params.get("start_token"),
        environment=params.get("environment")))


@_mobile_push_handler("mobile.session_activity.start_token.unregister")
def _(rid, params, principal):
    from tui_gateway.mobile_push import unregister_start_token_for_home
    # Revocation holds even when delivery is disabled or its credential is missing.
    return _ok(rid, {"removed": unregister_start_token_for_home(_hermes_home, principal,
        installation_id=params.get("installation_id"), connection_id=params.get("connection_id"))})


def _mobile_session_refresh(rid, params, principal, kind):
    service = _mobile_push_service()
    if service is None:
        return _err(rid, 4405, "mobile notifications are not configured")
    def accepts_scope(scope):
        if scope.surface != "native_session":
            return False
        try:
            destination, _tip, _title = _mobile_session_destination(
                {"profile": scope.profile, "stored_session_id": scope.session_id})
            return destination == scope
        except (ValueError, FileNotFoundError):
            return False
    options = dict(installation_id=params.get("installation_id"), connection_id=params.get("connection_id"),
                   token=params.get("activity_token" if kind == "activity" else "device_token"),
                   environment=params.get("environment"), kind=kind, accepts_scope=accepts_scope)
    if kind == "activity":
        options.update(activity_id=params.get("activity_id"), run_id=params.get("run_id"))
    else:
        options.update(categories=params.get("categories", ("attention", "completion")),
                       preview_enabled=params.get("preview_enabled", False))
    return _ok(rid, service.refresh(principal, **options))


@_mobile_push_handler("mobile.session_push.refresh")
def _(rid, params, principal):
    return _mobile_session_refresh(rid, params, principal, "alert")


@_mobile_push_handler("mobile.session_activity.refresh")
def _(rid, params, principal):
    return _mobile_session_refresh(rid, params, principal, "activity")


def _mobile_session_unregister(rid, params, principal, kind):
    from tui_gateway.mobile_push import unregister_for_home
    options = dict(installation_id=params.get("installation_id"), connection_id=params.get("connection_id"),
                   subscription_id=params.get("subscription_id"), kind=kind, surface="native_session")
    service = _mobile_push_service()
    removed = service.unregister(principal, **options) if service else unregister_for_home(_hermes_home, principal, **options)
    return _ok(rid, {"removed": removed})


@_mobile_push_handler("mobile.session_push.unregister")
def _(rid, params, principal):
    return _mobile_session_unregister(rid, params, principal, "alert")


@_mobile_push_handler("mobile.session_activity.unregister")
def _(rid, params, principal):
    if not params.get("subscription_id"):
        raise ValueError("subscription_id required")
    return _mobile_session_unregister(rid, params, principal, "activity")


@_mobile_push_handler("mobile.widget.unregister")
def _(rid, params, principal):
    return _mobile_session_unregister(rid, params, principal, "widget")


def _mobile_push_scope_for_session(session, refresh=False):
    from hermes_cli.profiles import list_profiles
    from tui_gateway.mobile_push import Scope
    if not refresh and session.get("_mobile_push_scope") is not None:
        return session["_mobile_push_scope"]
    session.pop("_mobile_push_scope", None)
    home = Path(session.get("profile_home") or _hermes_home).resolve()
    try:
        with _mobile_read_db(home) as db:
            root, tip, _chain = _mobile_canonical_identity(db)
    except ValueError:
        return _mobile_ordinary_push_scope(session, home)
    if session.get("session_key") != tip["id"]:
        return _mobile_ordinary_push_scope(session, home)
    profile = next((p for p in list_profiles() if Path(p.path).resolve() == home), None)
    if profile is not None:
        session["_mobile_push_scope"] = Scope("native", profile.name, root["id"])
        session["_mobile_push_listed"] = True  # a canonical Bot Chat is a Team row
        ui_meta = _read_profile_yaml(home).get("ui_meta", {})
        bot_meta = ui_meta.get("hermes-bots", {}) if isinstance(ui_meta, dict) else {}
        title = bot_meta.get("title") if isinstance(bot_meta, dict) else None
        session["_mobile_push_title"] = title or profile.display_name or profile.name
    return session.get("_mobile_push_scope")


def _mobile_ordinary_push_scope(session, home):
    from hermes_cli.profiles import list_profiles
    from tui_gateway.mobile_session_scope import listed_chat, resolve_destination
    profile = next((p for p in list_profiles() if Path(p.path).resolve() == home), None)
    if profile is None:
        return None
    destination, tip, title = resolve_destination(profile.name, home, session.get("session_key"))
    if session.get("session_key") != tip:
        return None
    session["_mobile_push_scope"] = destination
    session["_mobile_push_title"] = title
    session["_mobile_push_listed"] = listed_chat(home, destination.session_id)
    return destination


def _mobile_notification_run(profile, root_id):
    from tui_gateway.mobile_push import Scope
    try:
        service = _mobile_push_service()
        return service.current_run(Scope("native", profile, root_id)) if service else None
    except Exception as error:
        logger.warning("mobile push snapshot unavailable (%s)", type(error).__name__)
        return None


def _mobile_push_step(payload):
    """A few words for what a tool is doing; private (paths), so shown only with previews on."""
    name = str(payload.get("name") or "").lower().split("__")[-1]
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    path = args.get("path") or args.get("file_path")
    base = Path(path).name[:40] if isinstance(path, str) and path.strip() else ""
    if name in _EDIT_TOOLS:
        return f"Editing {base}" if base else "Editing files"
    if name == "read_file" and base:
        return f"Reading {base}"
    if name in {"terminal", "shell", "bash"}:
        command = str(args.get("command") or payload.get("context") or "")
        return "Running tests" if _TEST_COMMAND.search(command) else "Running a command"
    if name.startswith("browser"):
        return "Browsing"
    if name in _STEP_LABELS:
        return _STEP_LABELS[name]
    return ("Using " + name.replace("_", " "))[:40] if name else ""


def _mobile_push_agent_count(session):
    """Delegated agents this chat is waiting on, from the in-process registry (counts only)."""
    try:
        from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
        home = str(Path(session.get("profile_home") or _hermes_home).resolve())
        owners = {session.get("session_key"), getattr(session.get("agent"), "session_id", None)} - {None, ""}
        with _active_subagents_lock:
            return sum(1 for row in _active_subagents.values()
                       if row.get("owner_profile_home") == home and row.get("owner_conversation_id") in owners
                       and row.get("status") in {"running", "queued"})
    except Exception:
        return 0


def _mobile_push_plan(session):
    todos = (session.get("todo_state") or {}).get("todos") if isinstance(session.get("todo_state"), dict) else None
    if not isinstance(todos, list):
        return 0, 0
    states = [t.get("status") for t in todos if isinstance(t, dict) and t.get("status") != "cancelled"]
    return sum(1 for state in states if state == "completed"), len(states)


def _mobile_push_ask(event, payload):
    if event == "approval.request":
        return str(payload.get("description") or payload.get("command") or "")
    questions = payload.get("questions")
    if isinstance(questions, list) and questions and isinstance(questions[0], dict):
        return str(questions[0].get("question") or "")
    return str(payload.get("question") or "")


def _mobile_push_presentation(session, push_scope, *, reply="", ask=""):
    from tui_gateway.mobile_push_payloads import AlertPreview
    done, total = session.get("_mobile_push_plan") or (0, 0)
    return AlertPreview(title=session.get("_mobile_push_title") or push_scope.profile, reply=reply, ask=ask,
                        step=session.get("_mobile_push_step") or "", agents=session.get("_mobile_push_agents") or 0,
                        plan_done=done, plan_total=total)


def _mobile_push_capture(obj):
    """Persist before transport write, including detached sessions. Never await Apple here."""
    params = obj.get("params")
    if not isinstance(params, dict):
        return
    method = obj.get("method")
    if method == "event":
        event = params.get("type")
        payload = params.get("payload") or {}
    elif method in {"approval", "clarify"} and isinstance(obj.get("id"), str):
        event = f"{method}.request"
        payload = {**params, "request_id": obj["id"]}
    else:
        return
    if not isinstance(payload, dict) or event not in _MOBILE_PUSH_EVENTS:
        return
    try:
        session = _sessions.get(params.get("session_id"))
        if not session:
            return
        current = session.get("_mobile_push_status")
        # Token streams: nothing changes after the first delta of a phase, so skip all work.
        if (event, current) in {("message.delta", "responding"), ("thinking.delta", "thinking")}:
            return
        service = _mobile_push_service()
        if service is None:
            return
        push_scope = _mobile_push_scope_for_session(session, refresh=event == "message.start")
        if push_scope is None:
            return
        if event == "message.start":
            now = time.monotonic()
            pending = [t for t in session.get("_mobile_push_human_turns") or () if now - t < _MOBILE_PUSH_HUMAN_MARKER_TTL]
            human = bool(pending)
            session["_mobile_push_human_turns"] = pending[1:]
            # Continuations announce message.start before _run_prompt_submit announces it again.
            prior = session.get("_mobile_push_run_id") if current == "starting" else None
            session.update(_mobile_push_step="", _mobile_push_agents=_mobile_push_agent_count(session),
                           _mobile_push_plan=_mobile_push_plan(session))
            preview = _mobile_push_presentation(session, push_scope)
            run = service.start_run(push_scope, reuse_run_id=prior, listed=bool(session.get("_mobile_push_listed")),
                                    continuation=not human, preview=preview)
            if run["run_id"] != prior:
                session.update(_mobile_push_run_id=run["run_id"], _mobile_push_status="starting",
                               _mobile_push_sig=preview.to_json())
            return
        run_id = session.get("_mobile_push_run_id")
        if not run_id:
            return  # do not bind an old run to an unrelated runtime after restart
        if event == "todo.updated":
            if current is None or current in {"complete", "failed", "cancelled"}:
                return
            status = current
            session["_mobile_push_plan"] = _mobile_push_plan({"todo_state": payload})
        elif event == "request.cancel":
            if payload.get("method") not in {"approval", "clarify"}:
                return
            from tui_gateway.server_requests import pending_kind
            kind = pending_kind(params.get("session_id"))
            status = {"approval": "waitingForApproval", "clarify": "waitingForClarification"}.get(kind, "thinking")
        elif event == "message.complete":
            status = {"error": "failed", "interrupted": "cancelled"}.get(payload.get("status"), "complete")
        else:
            status = _MOBILE_PUSH_STATUSES[event]
        if event == "tool.start":
            session["_mobile_push_step"] = _mobile_push_step(payload)
        elif event != "todo.updated" and status != "usingTool":
            session["_mobile_push_step"] = ""
        if event in {"tool.start", "tool.complete"}:
            session["_mobile_push_agents"] = _mobile_push_agent_count(session)
        preview = _mobile_push_presentation(
            session, push_scope, reply=payload.get("text", "") if status == "complete" else "",
            ask=_mobile_push_ask(event, payload) if event in {"approval.request", "clarify.request"} else "")
        signature = preview.to_json()
        if (status == current and signature == session.get("_mobile_push_sig")
                and event not in {"approval.request", "clarify.request"}):
            return
        request_id = payload.get("id") if event == "request.cancel" else payload.get("request_id")
        event_id = f"{event}:{request_id}" if request_id else f"{event}:{params.get('seq', 0)}"
        service.record(push_scope, run_id, event_id, status, preview=preview)
        session.update(_mobile_push_status=status, _mobile_push_sig=signature)
    except Exception as error:
        # Delivery is a projection. A disk/provider problem must not terminate the turn,
        # and exception messages may contain a path or token, so log only their class.
        logger.warning("mobile push event projection unavailable (%s)", type(error).__name__)


def _mobile_push_finish(session, status):
    try:
        service = _mobile_push_service()
        scope = session.get("_mobile_push_scope")
        run_id = session.get("_mobile_push_run_id")
        if service and scope and run_id:
            terminal = {"error": "failed", "interrupted": "cancelled"}.get(status, "complete")
            service.record(scope, run_id, "turn-finally", terminal)
            session["_mobile_push_status"] = terminal
    except Exception as error:
        logger.warning("mobile push terminal projection unavailable (%s)", type(error).__name__)


def _mobile_push_note_submission(session, params, status="streaming"):
    """KTD7: an accepted human prompt names the chat's latest human origin.

    A phone stamps ``origin`` {installation_id, connection_id} on an authenticated transport and
    becomes eligible for push-to-start; any other human submission (Desktop) clears eligibility.
    Relayed or hosted turns are not human input and leave it alone. A prompt that will run as its
    own turn (streaming, queued) marks that turn's message.start as human however long it waits in
    the queue; steered or redirected input joins the running turn and marks nothing.
    """
    if params.get("_turn_author") is not None or params.get("_hosted_task") is not None:
        return
    try:
        service = _mobile_push_service()
        if service is None:
            return
        if status in {"streaming", "queued"}:
            session["_mobile_push_human_turns"] = [*(session.get("_mobile_push_human_turns") or ()), time.monotonic()]
        push_scope = _mobile_push_scope_for_session(session)
        if push_scope is None:
            return
        origin = params.get("origin")
        identity = getattr(current_transport(), "auth_identity", None)
        from tui_gateway.methods_browser_control import _is_authenticated_identity, _principal_digest
        if isinstance(origin, dict) and _is_authenticated_identity(identity):
            try:
                service.store.set_origin(push_scope, _principal_digest(identity),
                                         installation_id=origin.get("installation_id"),
                                         connection_id=origin.get("connection_id"))
                return
            except ValueError:
                pass  # a malformed origin is not a phone: clear rather than keep a stale one
        service.store.set_origin(push_scope)
    except Exception as error:
        logger.warning("mobile push origin unavailable (%s)", type(error).__name__)


def _mobile_push_live_runs():
    """(held, running) run ids for the orphan sweep; sessions are read, never locked."""
    sessions = list(_sessions.values())
    held = {s.get("_mobile_push_run_id") for s in sessions if s.get("_mobile_push_run_id")}
    running = {s.get("_mobile_push_run_id") for s in sessions if s.get("_mobile_push_run_id") and s.get("running")}
    return held, running


def register(server):
    bind_module(globals(), server, skip=("_",))
    server._MOBILE_METHODS += _MOBILE_PUSH_METHODS + ("session.stream.snapshot",)
    # Resume a persisted outbox when this existing backend starts, before any new events.
    service = server._mobile_push_service()
    if service is not None:
        service.live_runs = server._mobile_push_live_runs
