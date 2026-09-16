"""Existing canonical Bot Chats for native clients; no creation or migration authority."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_MOBILE_METHODS = (
    "mobile.capabilities", "mobile.bots", "mobile.open", "mobile.snapshot",
    "mobile.submit", "mobile.stop", "mobile.approval.respond", "mobile.clarify.respond",
)


def _mobile_handler(name):
    def decorate(fn):
        def handle(rid, params):
            import sqlite3
            try:
                return fn(rid, params)
            except (ValueError, FileNotFoundError):
                return _err(rid, 4400, "invalid mobile scope or unavailable canonical Bot Chat")
            except (sqlite3.Error, RuntimeError):
                return _err(rid, 4401, "canonical state unavailable; reconnect and reopen Bot Chat")
        return method(name)(handle)
    return decorate


def _mobile_string(params, name):
    value = params.get(name)
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise ValueError(name)
    return value


def _mobile_limit(params, name="limit", default=50, maximum=100, minimum=1):
    value = params.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(name)
    return value


def _mobile_profile(params):
    from hermes_cli.profiles import get_profile_dir, profile_exists, validate_profile_name
    name = _mobile_string(params, "profile")
    validate_profile_name(name)
    home = Path(get_profile_dir(name))
    if not profile_exists(name) or not home.is_dir():
        raise FileNotFoundError(name)
    return name, home


def _mobile_canonical_identity(db, root_id=None):
    """Reject duplicate registry rows and retired chats; never resurrect them during reads."""
    from hermes_state import SessionDB
    with db._lock:
        rows = db._conn.execute("SELECT id FROM sessions WHERE title = ? LIMIT 2", (SessionDB.CANONICAL_BOT_CHAT_TITLE,)).fetchall()
    if len(rows) != 1:
        raise ValueError("canonical absent or ambiguous")
    root = db.get_session(rows[0][0])
    if not root or root.get("archived") or _denied_source(root):
        raise ValueError("canonical unavailable")
    if root_id is not None and root["id"] != root_id:
        raise ValueError("canonical identity mismatch")
    chain = db.get_compression_chain(root["id"])
    if not chain or chain[0] != root["id"]:
        raise ValueError("canonical root is not a compression root")
    tip = db.get_session(chain[-1])
    if not tip or tip.get("archived") or _denied_source(tip):
        raise ValueError("canonical tip unavailable")
    return root, tip, chain


def _mobile_read_db(home):
    from hermes_state import SessionDB
    from hermes_state_common import stat_db_file_identity
    from tui_gateway.mobile_history_cache import history_pages
    path = home / "state.db"
    history_pages.invalidate_replaced(path)
    if not path.is_file():
        raise FileNotFoundError("profile state unavailable")
    identity = stat_db_file_identity(path)
    db = SessionDB(db_path=path, read_only=True)
    if stat_db_file_identity(path) != identity:
        db.close()
        raise RuntimeError("profile state replaced during open")
    return db


def _mobile_scope(params):
    """Validate the exact runtime object and current peer, then its current profile/root."""
    profile, home = _mobile_profile(params)
    root_id = _mobile_string(params, "canonical_root_id")
    sid = _mobile_string(params, "session_id")
    session = _sessions.get(sid)
    if not session or not _session_transport_contains(session, current_transport()):
        raise ValueError("runtime is not attached to this connection")
    actual_home = Path(session.get("profile_home") or _hermes_home).resolve()
    if actual_home != home.resolve():
        raise ValueError("runtime profile mismatch")
    with _mobile_read_db(home) as db:
        root, tip, chain = _mobile_canonical_identity(db, root_id)
    if session.get("session_key") != tip["id"]:
        raise ValueError("runtime changed; reopen canonical chat")
    # The approval registry is keyed by durable ID. Ambiguous cross-profile IDs cannot answer it.
    for other in list(_sessions.values()):
        if (other is not session and other.get("session_key") == session.get("session_key")
                and Path(other.get("profile_home") or _hermes_home).resolve() != home.resolve()):
            raise ValueError("durable request scope is ambiguous")
    return profile, home, sid, session, root, tip, chain


@_mobile_handler("mobile.capabilities")
def _(rid, params):
    return _ok(rid, {"protocol_version": 1, "methods": list(_MOBILE_METHODS),
        "features": ["strict_canonical_open", "bounded_history", "scoped_interactions",
                     "queued_submit", "snapshot_reconciliation"],
        "max_history_limit": 100, "max_roster_limit": 100})


@_mobile_handler("mobile.bots")
def _(rid, params):
    import sqlite3
    from hermes_cli.profiles import list_profiles
    limit = _mobile_limit(params)
    offset = _mobile_limit(params, "offset", 0, 100000, 0)
    profiles = list_profiles()
    live_by_profile_and_session = {}
    for session in list(_sessions.values()):
        identity = (Path(session.get("profile_home") or _hermes_home).resolve(), session.get("session_key"))
        live_by_profile_and_session.setdefault(identity, session)
    out = []
    for profile in profiles[offset:offset + limit]:
        home = Path(profile.path)
        raw = _read_profile_yaml(home).get("ui_meta", {})
        meta = raw.get("hermes-bots", {}) if isinstance(raw, dict) else {}
        meta = meta if isinstance(meta, dict) else {}
        safe_meta = {key: value[:maximum] for key, maximum in (
            ("title", 160), ("description", 500), ("color", 64), ("shape", 120), ("emoji", 32))
            if isinstance(value := meta.get(key), str)}
        row = {"profile": profile.name, "display_name": str(profile.display_name or "")[:160],
               "description": str(profile.description or "")[:500], "title": safe_meta.get("title", ""),
               "ui_meta": {"hermes-bots": safe_meta}, "canonical_session": None,
               "has_avatar": any((home / "assets" / f"avatar.{ext}").is_file() for ext in ("png", "jpg", "webp")),
               "working": None}
        try:
            with _mobile_read_db(home) as db:
                root, tip, _chain = _mobile_canonical_identity(db)
                row["canonical_session"] = {"id": root["id"], "resolved_id": tip["id"],
                    "preview": _latest_message_preview(db, tip["id"]),
                    "last_active": tip.get("last_activity_at") or tip.get("started_at") or 0,
                    "message_count": tip.get("message_count") or 0}
            live = live_by_profile_and_session.get((home.resolve(), tip["id"]))
            if live is not None:
                row["working"] = bool(live.get("running"))
        except (ValueError, FileNotFoundError, sqlite3.Error, RuntimeError):
            row["unavailable_reason"] = "canonical_unavailable"
        out.append(row)
    return _ok(rid, {"bots": out, "next_offset": offset + limit if offset + limit < len(profiles) else None})


def _mobile_history_page(db, tip_id, chain, params):
    """Bounded output, canonical generation dedupe in SQLite; cursor is logical first-row ID.

    SQLite may scan the lineage to deduplicate, but Python never materializes an unbounded transcript.
    The chosen durable row can change after compaction; logical ordering and pagination stay stable.
    Unchanged polls reuse bounded row pages; every database commit invalidates that reuse.
    """
    from agent.context_compressor import split_user_originated_turn
    from tui_gateway.mobile_history_cache import history_pages
    limit = _mobile_limit(params)
    before = _mobile_limit(params, "before_row_id", 2**63 - 1, 2**63 - 1)

    def content_key(role, content, kind, metadata):
        if role == "user":
            handoff, live = split_user_originated_turn({"role": role, "content": db._decode_content(content),
                "display_kind": kind, "display_metadata": db._decode_display_metadata(metadata)})
            if handoff is not None and live is not None:
                return db._encode_content(live.get("content"))
        return content

    slots = ",".join("?" for _ in chain)
    partition = "role, mobile_content_key(role, content, display_kind, display_metadata), timestamp, tool_call_id, tool_calls, tool_name"
    page_key = (tip_id, tuple(chain), before, limit)
    with db._lock:
        token, rows = history_pages.lookup(db, page_key)
        if rows is None:
            db._conn.create_function("mobile_content_key", 4, content_key, deterministic=True)
            rows = db._conn.execute(f"""
                WITH ranked AS (
                    SELECT *, MIN(id) OVER (PARTITION BY {partition}) AS logical_id,
                        ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY active DESC, id DESC) AS generation_rank
                    FROM messages WHERE session_id IN ({slots}) AND (active = 1 OR compacted = 1)
                ) SELECT * FROM ranked WHERE generation_rank = 1 AND logical_id < ?
                  ORDER BY logical_id DESC LIMIT ?
            """, (*chain, before, limit + 1)).fetchall()
            history_pages.publish(token, page_key, rows)
    has_more = len(rows) > limit
    selected = list(reversed(rows[:limit]))
    messages = []
    for row in selected:
        history = db._rows_to_conversation([row], session_id=tip_id, include_ancestors=False,
                                           repair_alternation=False, include_row_ids=True)
        projected = _history_to_messages(history)
        tool_calls = history[0].get("tool_calls") if history else None
        if not projected and row["role"] == "assistant" and tool_calls and row["display_kind"] != "hidden":
            projected = [{"role": "assistant", "text": ""}]
        # Existing compact tool cards omit the result. Mobile history retains its full text too.
        for ordinal, message in enumerate(projected):
            message["row_id"] = row["id"]
            message["message_id"] = f"{row['id']}:{ordinal}"
            if tool_calls:
                message["tool_calls"] = tool_calls
            if message.get("role") == "tool":
                message["text"] = _coerce_message_text(db._decode_content(row["content"]))
                message["timestamp"] = row["timestamp"]
                message["tool_call_id"] = row["tool_call_id"]
        messages.extend(projected)
    return {"messages": messages, "has_more": has_more,
            "before_row_id": selected[0]["logical_id"] if has_more and selected else None}


def _mobile_approvals(session, requests=()):
    from tools.approval import list_gateway_approvals
    out = []
    for pending in list_gateway_approvals(session["session_key"]):
        safe = _approval_request_payload(pending)
        safe["choices"] = [choice for choice in safe.get("choices", []) if choice in ("once", "deny")]
        out.append(safe)
    queue_ids = {pending.get("request_id") for pending in out}
    for request in requests:
        if request["method"] == "approval" and request["params"].get("request_id") not in queue_ids:
            safe = _approval_request_payload(request["params"])
            safe["request_id"] = request["id"]
            safe["choices"] = [choice for choice in safe.get("choices", []) if choice in ("once", "deny")]
            out.append(safe)
    return out


def _mobile_snapshot(params):
    profile, home, sid, session, root, tip, chain = _mobile_scope(params)
    with _mobile_read_db(home) as db:
        history = _mobile_history_page(db, tip["id"], chain, params)
    with session["history_lock"]:
        inflight, queued = _inflight_snapshot(session), _queued_prompt_snapshot(session)
        running = bool(session.get("running"))
    requests = _open_requests(sid)
    pending_clarify = next(({**request["params"], "request_id": request["id"]}
                            for request in requests if request["method"] == "clarify"), None)
    if pending_clarify is not None and session.get("_compute_host_open_request", {}).get("id") == pending_clarify["request_id"]:
        pending_clarify.update(mobile_supported=False, owning_client="desktop")
    return _attach_todo_state({"profile": profile, "canonical_root_id": root["id"], "session_id": sid,
            "stored_session_id": tip["id"], "history": history, "running": running,
            "status": _session_live_status(sid, session), "inflight": inflight, "queued": queued,
            "pending_approvals": _mobile_approvals(session, requests),
            "pending_clarify": pending_clarify, "open_requests": requests,
            "notification_run": _mobile_notification_run(profile, root["id"])}, session)


@_mobile_handler("mobile.open")
def _(rid, params):
    profile, home = _mobile_profile(params)
    root_id = _mobile_string(params, "canonical_root_id")
    _mobile_limit(params)
    if current_transport() is None:
        raise ValueError("connection required")
    with _mobile_read_db(home) as db:
        _mobile_canonical_identity(db, root_id)
    # Strictness is enforced again INSIDE resume, before compatibility lookup/adoption.
    response = _methods["session.resume"](rid, {"profile": profile, "session_id": root_id,
        "strict_canonical_root_id": root_id, "defer_history": True, "omit_messages": True,
        "close_on_disconnect": False, "source": "desktop"})
    if "error" in response:
        return response
    return _ok(rid, _mobile_snapshot({**params, "session_id": response["result"]["session_id"]}))


@_mobile_handler("mobile.snapshot")
def _(rid, params):
    return _ok(rid, _mobile_snapshot(params))


@_mobile_handler("mobile.submit")
def _(rid, params):
    from hermes_cli.input_sanitize import sanitize_user_prompt_text
    text = params.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 256 * 1024:
        raise ValueError("text required or too large")
    if "\x1b" in text:
        return _err(rid, 4403, "terminal control sequences are unavailable in mobile Bot Chat")
    text = sanitize_user_prompt_text(text)
    if text.lstrip().startswith("/"):
        return _err(rid, 4403, "slash commands are unavailable in mobile Bot Chat")
    _profile, _home, sid, session, *_rest = _mobile_scope(params)
    response = _methods["prompt.submit"](rid, {"session_id": sid, "text": text, "queued": True,
        "_mobile_expected_record": session, "_mobile_scope_params": params})
    if "result" in response:
        response["result"]["accepted"] = response["result"].get("status") in ("streaming", "queued")
    return response


@_mobile_handler("mobile.stop")
def _(rid, params):
    _profile, _home, sid, session, *_rest = _mobile_scope(params)
    return _methods["session.interrupt"](rid, {"session_id": sid,
        "_mobile_expected_record": session, "_mobile_scope_params": params})


@_mobile_handler("mobile.approval.respond")
def _(rid, params):
    request_id = _mobile_string(params, "request_id")
    choice = _mobile_string(params, "choice")
    with _session_resume_lock:
        _profile, _home, sid, session, *_rest = _mobile_scope(params)
        with session["history_lock"]:
            _mobile_scope(params)
            if request_id.startswith("srq-"):
                from tui_gateway import server_requests
                try:
                    resolved = server_requests.resolve_response(
                        {"id": request_id, "result": {"choice": choice}},
                        expected_sid=sid, expected_method="approval", mobile=True)
                except (PermissionError, ValueError) as exc:
                    return _err(rid, 4403, str(exc))
                return _ok(rid, {"resolved": int(resolved)})
            return _mobile_approval_respond(rid, session, request_id, choice)


@_mobile_handler("mobile.clarify.respond")
def _(rid, params):
    request_id = _mobile_string(params, "request_id")
    if not isinstance(params.get("answer"), str):
        raise ValueError("answer required")
    with _session_resume_lock:
        _profile, _home, sid, session, *_rest = _mobile_scope(params)
        # Compute-host prompts need a separately fenced worker response; do not search other runtimes.
        if session.get("_compute_host_open_request", {}).get("id") == request_id:
            return _err(rid, 4404, "this clarification must be answered on its owning client")
        with session["history_lock"]:
            _mobile_scope(params)
            return _respond(rid, {"request_id": request_id, "answer": params["answer"],
                                 "question_id": params.get("question_id", "")}, "answer",
                            allow_expired=True, expected_sid=sid, expected_event="clarify.request")


def register(server):
    bind_module(globals(), server, skip=("_",))
    server._LONG_HANDLERS = server._LONG_HANDLERS | {"mobile.bots", "mobile.open", "mobile.snapshot"}
