"""Authenticated registrations and canonical native event projection, never approval execution."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_MOBILE_PUSH_METHODS = (
    "mobile.push.status", "mobile.push.register", "mobile.push.unregister",
    "mobile.activity.register", "mobile.activity.unregister",
    "mobile.push.refresh", "mobile.activity.refresh",
)
_MOBILE_PUSH_STATUSES = {
    "tool.start": "usingTool", "tool.complete": "thinking", "message.delta": "responding",
    "thinking.delta": "thinking", "approval.request": "waitingForApproval",
    "clarify.request": "waitingForClarification",
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


def _mobile_push_scope_for_session(session, refresh=False):
    from hermes_cli.profiles import list_profiles
    from tui_gateway.mobile_push import Scope
    if not refresh and session.get("_mobile_push_scope") is not None:
        return session["_mobile_push_scope"]
    session.pop("_mobile_push_scope", None)
    home = Path(session.get("profile_home") or _hermes_home).resolve()
    with _mobile_read_db(home) as db:
        root, tip, _chain = _mobile_canonical_identity(db)
    if session.get("session_key") != tip["id"]:
        return None
    profile = next((p for p in list_profiles() if Path(p.path).resolve() == home), None)
    if profile is not None:
        session["_mobile_push_scope"] = Scope("native", profile.name, root["id"])
        ui_meta = _read_profile_yaml(home).get("ui_meta", {})
        bot_meta = ui_meta.get("hermes-bots", {}) if isinstance(ui_meta, dict) else {}
        title = bot_meta.get("title") if isinstance(bot_meta, dict) else None
        session["_mobile_push_title"] = title or profile.display_name or profile.name
    return session.get("_mobile_push_scope")


def _mobile_notification_run(profile, root_id):
    from tui_gateway.mobile_push import Scope
    try:
        service = _mobile_push_service()
        return service.current_run(Scope("native", profile, root_id)) if service else None
    except Exception as error:
        logger.warning("mobile push snapshot unavailable (%s)", type(error).__name__)
        return None


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
    if not isinstance(payload, dict):
        return
    if event not in _MOBILE_PUSH_STATUSES and event not in {"message.start", "message.complete", "request.cancel"}:
        return
    try:
        service = _mobile_push_service()
        session = _sessions.get(params.get("session_id"))
        if service is None or not session:
            return
        push_scope = _mobile_push_scope_for_session(session, refresh=event == "message.start")
        if push_scope is None:
            return
        if event == "message.start":
            run = service.start_run(push_scope)
            session["_mobile_push_run_id"] = run["run_id"]
            session["_mobile_push_status"] = "starting"
            return
        run_id = session.get("_mobile_push_run_id")
        if not run_id:
            return  # do not bind an old run to an unrelated runtime after restart
        if event == "request.cancel":
            if payload.get("method") not in {"approval", "clarify"}:
                return
            from tui_gateway.server_requests import pending_kind
            kind = pending_kind(params.get("session_id"))
            status = {"approval": "waitingForApproval", "clarify": "waitingForClarification"}.get(kind, "thinking")
        elif event == "message.complete":
            status = {"error": "failed", "interrupted": "cancelled"}.get(payload.get("status"), "complete")
        else:
            status = _MOBILE_PUSH_STATUSES[event]
        if status == session.get("_mobile_push_status") and event not in {"approval.request", "clarify.request"}:
            return
        request_id = payload.get("id") if event == "request.cancel" else payload.get("request_id")
        event_id = f"{event}:{request_id}" if request_id else f"{event}:{params.get('seq', 0)}"
        from tui_gateway.mobile_push_payloads import AlertPreview
        title = session.get("_mobile_push_title") or push_scope.profile
        preview = AlertPreview(title=title, reply=payload.get("text", "") if status == "complete" else "")
        service.record(push_scope, run_id, event_id, status, preview=preview)
        session["_mobile_push_status"] = status
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
            service.record(scope, run_id, "turn-finally",
                {"error": "failed", "interrupted": "cancelled"}.get(status, "complete"))
    except Exception as error:
        logger.warning("mobile push terminal projection unavailable (%s)", type(error).__name__)


def register(server):
    bind_module(globals(), server, skip=("_",))
    server._MOBILE_METHODS += _MOBILE_PUSH_METHODS
    # Resume a persisted outbox when this existing backend starts, before any new events.
    server._mobile_push_service()
