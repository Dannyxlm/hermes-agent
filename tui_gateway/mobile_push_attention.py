"""Safe attention projection, never an answer authority or a credential-bearing queue.

Descriptors are selectors for a fresh owner-authenticated read. The canonical registry and
compute mirrors own srqs; only connector sequence tombstones are retained between events.
"""
import math

from .human_input import HUMAN_METHODS, SECURE_METHODS

CATEGORIES = {"approval": "HERMEX_APPROVAL_V1", "clarify": "HERMEX_QUESTION_V1",
              "setup_choose": "HERMEX_SETUP_V1", "connection.request": "HERMEX_SETUP_V1"}
SECURE_CATEGORY = "HERMEX_SECURE_INPUT_V1"
# Unknown, destructive, payment and tool-escalation approvals require foreground review.
_QUICK_TOOLS = frozenset({"read_file", "search_files", "web_search", "web_extract"})


def _time(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def descriptor(frame):
    """Explicit metadata allowlist; never copy a producer's dict into a persisted payload."""
    method, params = frame.get("method"), frame.get("params", {})
    if method not in HUMAN_METHODS or not isinstance(params, dict):
        return None
    ident, epoch, revision = frame.get("id"), params.get("request_epoch"), params.get("request_revision")
    canonical = isinstance(ident, str) and isinstance(epoch, str) and isinstance(revision, int)
    category = SECURE_CATEGORY if method in SECURE_METHODS else CATEGORIES[method]
    target = {"version": 1, "request_id": ident, "method": method,
              "session_id": params.get("session_id", ""), "request_epoch": epoch,
              "request_revision": revision, "created_at": _time(params.get("created_at")),
              "expires_at": _time(params.get("expires_at")), "category": category,
              "allowed_actions": ["open_securely"] if method in SECURE_METHODS else ["open"]}
    if not canonical:
        target.pop("request_epoch")
        target.pop("request_revision")
        return target  # generic attention/open only for legacy producers
    if method == "approval":
        choices = params.get("choices") or []
        if "deny" in choices:
            target["allowed_actions"] += ["deny", "suggest"]
        if "once" in choices and params.get("tool_name") in _QUICK_TOOLS and not params.get("surface"):
            target["allowed_actions"].append("once")
    elif method == "clarify":
        remaining = [q for q in params.get("questions", []) if q.get("qid") not in (params.get("answers") or {})]
        if remaining:
            target["question_id"] = remaining[0]["qid"]
        if len(remaining) == 1 and not remaining[0].get("multi_select"):
            target["allowed_actions"] += ["reply", "skip"]
    elif method == "setup_choose":
        target["allowed_actions"].append("skip")
    return target


def note_connection(session, event, payload, now):
    """Monotonic per-operation metadata, including tombstones; never targets, env or URLs."""
    if event not in {"connection.request", "connection.update"}:
        return
    ident, seq = payload.get("op_id"), payload.get("seq")
    if not isinstance(ident, str) or not isinstance(seq, int):
        return
    operations = session.setdefault("_mobile_push_operations", {})
    previous = operations.get(ident)
    if previous is not None and previous["request_revision"] >= seq:
        return
    from .server_requests import inbox_epoch
    secure = any(t.get("required_env") for t in payload.get("targets", []) if isinstance(t, dict))
    operations[ident] = {"version": 1, "operation_id": ident, "method": "connection.request",
        "request_epoch": inbox_epoch(), "request_revision": seq,
        "created_at": previous["created_at"] if previous else now,
        "expires_at": _time(payload.get("deadline_at")),
        "category": SECURE_CATEGORY if secure else CATEGORIES["connection.request"],
        "allowed_actions": ["open_securely"] if secure else ["open"],
        "settled": bool(payload.get("settled") or payload.get("settled_at"))}


def request_frames(session, sid):
    from .server_requests import open_requests
    frames = {frame["id"]: frame for frame in open_requests(sid)}
    frames.update(session.get("_compute_host_open_requests") or {})
    legacy = session.get("_compute_host_open_request")
    if legacy:
        frames.setdefault(legacy["id"], legacy)
    retired = session.get("_mobile_push_retired_requests", set())
    return {ident: frame for ident, frame in frames.items() if ident not in retired}


def pending_projection(session, sid, now):
    frames = request_frames(session, sid)
    targets = [target for frame in frames.values() if (target := descriptor(updated_frame(session, frame))) is not None]
    targets += [{k: v for k, v in target.items() if k != "settled"} | {"session_id": sid}
                for target in session.get("_mobile_push_operations", {}).values() if not target["settled"]]
    targets = [t for t in targets if t["expires_at"] is None or t["expires_at"] > now]
    targets.sort(key=lambda t: (t["created_at"] or 0, t.get("request_id", t.get("operation_id", ""))))
    return targets


def note_update(session, event, payload):
    updates = session.setdefault("_mobile_push_request_updates", {})
    if event == "request.cancel" and payload.get("method") in HUMAN_METHODS:
        updates.pop(payload.get("id"), None)
        session.setdefault("_mobile_push_retired_requests", set()).add(payload.get("id"))
    elif event == "request.updated":
        ident = payload.get("id")
        mirrors = session.get("_compute_host_open_requests", {})
        if ident not in mirrors and (session.get("_compute_host_open_request") or {}).get("id") != ident:
            return
        previous = updates.get(ident, {})
        if payload.get("request_revision", 0) > previous.get("request_revision", 0):
            updates[ident] = {key: payload[key] for key in (
                "request_epoch", "request_revision", "remaining_question_ids")}


def updated_frame(session, frame):
    update = session.get("_mobile_push_request_updates", {}).get(frame["id"])
    params = frame["params"]
    if (update and update["request_epoch"] == params.get("request_epoch")
            and update["request_revision"] > params.get("request_revision", 0)):
        locked = {q["qid"]: None for q in params.get("questions", [])
                  if q["qid"] not in update["remaining_question_ids"]}
        return {**frame, "params": {**params, "answers": locked, "request_revision": update["request_revision"]}}
    return frame


def question_preview(session, sid, attention):
    if not attention or attention["primary"]["method"] != "clarify":
        return ""
    target = attention["primary"]
    frame = request_frames(session, sid).get(target.get("request_id"), {})
    return next((q.get("question", "") for q in frame.get("params", {}).get("questions", [])
                 if q.get("qid") == target.get("question_id")), "")


def attention_state(targets):
    """One deterministic oldest descriptor; nearest expiry can belong to a different request."""
    if not targets:
        return None
    deadlines = [t["expires_at"] for t in targets if t["expires_at"] is not None]
    return {"count": len(targets), "primary": targets[0], "nearest_expires_at": min(deadlines) if deadlines else None}


def external_attention(sids):
    """Inbox reads canonical connector owners and compute mirrors even with APNs disabled."""
    import sys
    import time
    from tools.connectors.live import current
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return {}
    result = {}
    for sid in sids:
        session = server._sessions.get(sid)
        if session is None:
            continue
        entries = []
        for frame in request_frames(session, sid).values():
            target = descriptor(frame)
            if target and (target["expires_at"] is None or target["expires_at"] > time.time()):
                kind = {"approval": "approval", "clarify": "clarify"}.get(target["method"], "input")
                entries.append((frame["id"], kind))
        op = current(session.get("session_key"), profile_home=session.get("profile_home") or server._hermes_home)
        if op is not None and op.remaining_seconds() > 0:
            entries.append((op.op_id, "input"))
        # Child connector operations exist only in the child; its monotonic event cut is the mirror.
        if op is None:
            entries.extend((target["operation_id"], "input") for target in pending_projection(session, sid, time.time())
                           if "operation_id" in target)
        if entries:
            result[sid] = entries
    return result


def project_connection_attention(service):
    """Sessionless sudo uses existing profile attention consent, never an arbitrary chat.

    Borrow an enrolled alert lease only for delivery (one per installation/connection).
    No derived subscription survives revocation; no Live Activity is created for host setup.
    """
    import hashlib
    import json
    from pathlib import Path
    from hermes_cli.profiles import list_profiles
    from .mobile_inbox_invalidation import note_profile
    from .mobile_push_payloads import AlertPreview, Scope
    from .server_requests import scoped_pending
    store = service.store
    # Serialize connection cuts with enqueue/cancel, including in-flight retry retirement.
    with store._lock:
        for profile in list_profiles():
            frames = scoped_pending("", profile_home=str(Path(profile.path).resolve()))
            attention = attention_state([target for frame in frames if (target := descriptor(frame)) is not None])
            scope = Scope("connection", profile.name, "connection")
            current = store.current_run(scope)
            if not attention:
                if current and current["status"] not in {"complete", "failed", "cancelled"}:
                    with store.transaction() as db:
                        store._close(db, current, "resolved")
                    note_profile(profile.name)
                continue
            note_profile(profile.name)
            if not current or current["status"] in {"complete", "failed", "cancelled"}:
                current = service.start_run(scope)
            preview = AlertPreview(attention=attention)
            event_id = "connection-input:" + hashlib.sha256(json.dumps(attention, sort_keys=True).encode()).hexdigest()
            if not service.record(scope, current["run_id"], event_id, status_for(attention), preview=preview):
                continue
            run = store.current_run(scope)
            with store.transaction() as db:
                rows = db.execute("""SELECT * FROM subscriptions WHERE profile=? AND kind='alert'
                    AND expires_at>? AND EXISTS (SELECT 1 FROM json_each(categories) WHERE value='attention')
                    ORDER BY id""", (profile.name, store.clock())).fetchall()
                seen = set()
                for sub in rows:
                    identity = (sub["principal"], sub["installation_id"], sub["connection_id"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    store._enqueue(db, sub, run, scope, event_id, preview=preview)


def status_for(attention):
    return "waitingForApproval" if attention["primary"]["method"] == "approval" else "waitingForClarification"


def deadline(preview):
    return (preview.attention or {}).get("nearest_expires_at") if preview else None


def freshness(aps, now):
    """Freshness expiry is not human expiry; neither can extend the request deadline."""
    content = aps.get("content-state") or {}
    pending = content.get("pendingInput") or {}
    expires = _time(content.get("nearestExpiresAt")) or _time(pending.get("expires_at"))
    return int(min(now + 120, expires)) if expires is not None else int(now + 120)
