"""Read-only Inbox projections; no runtime hydration or delivery enrollment."""
from pathlib import Path
import sys

from . import server_requests


def summary_scope(profile, *, replies_complete=None):
    server = sys.modules.get("tui_gateway.server")
    scope = {"profile": profile, "pending_scope": "process", "pending_complete": server is not None}
    if server is not None:
        scope["pending_epoch"] = server_requests.inbox_epoch()
    if replies_complete is not None:
        scope["replies_complete"] = replies_complete
    return scope


def latest_run(session, launch_home, stored_id):
    """Use the ordinary producer's exact destination, not a cached runtime run id."""
    import logging
    import sqlite3
    from hermes_constants import profile_name_for_home
    from .mobile_session_scope import resolve_destination
    from .mobile_push_store import PushStore
    path = Path(launch_home) / "mobile-push" / "outbox.sqlite3"
    if not path.is_file():
        return None
    home = Path(session.get("profile_home") or launch_home)
    try:
        scope, _tip, _title = resolve_destination(profile_name_for_home(home), home, stored_id)
        run = PushStore.current_run_readonly(path, scope)
    except (OSError, ValueError, sqlite3.Error) as error:
        logging.getLogger(__name__).warning("Inbox run summary unavailable (%s)", type(error).__name__)
        return None
    return {"run_id": run["run_id"], "status": run["status"], "at": run["updated_at"]} if run else None


def page_replies(db, rows):
    """One indexed message query for this page's already-resolved compression chains."""
    lineages = {row["id"]: row.get("_lineage_ids") or [row["id"]] for row in rows}
    ids = list({sid for chain in lineages.values() for sid in chain})
    if not ids:
        return {}
    latest = db._read_all(f"""
        SELECT m.session_id, m.id, m.timestamp FROM messages m
        JOIN (SELECT session_id, MAX(id) AS max_id FROM messages
              WHERE session_id IN ({','.join('?' for _ in ids)})
                AND role = 'assistant'
                AND COALESCE(finish_reason, '') != 'tool_calls'
                AND length(trim(COALESCE(content, ''), char(9)||char(10)||char(13)||' ')) > 0
              GROUP BY session_id) final ON m.id = final.max_id
    """, ids)
    by_id = {r["session_id"]: r for r in latest}
    result = {}
    for key, chain in lineages.items():
        candidates = [by_id[sid] for sid in chain if sid in by_id]
        if candidates:
            final = max(candidates, key=lambda r: r["id"])
            result[key] = {"row_id": final["id"], "at": final["timestamp"]}
    return result


def page_attention(rows, home):
    """Only an already-loaded native supervisor can lend process-local evidence."""
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return {}
    ids = {row["id"] for row in rows}
    bindings = {sid: server._session_lookup_key(session, fallback=sid)
                for sid, session in list(server._sessions.items())
                if not session.get("_finalized")
                and Path(session.get("profile_home") or server._hermes_home).resolve() == Path(home).resolve()
                and server._session_lookup_key(session, fallback=sid) in ids}
    summaries = server_requests.attention_summaries(list(bindings))
    # Normally one runtime per stored id. If there are several, merge the actual
    # request sets rather than arbitrarily choosing a runtime.
    grouped = {}
    for sid, summary in summaries.items():
        grouped.setdefault(bindings[sid], []).append(summary)
    import hashlib
    priority = {"approval": 0, "clarify": 1, "input": 2}
    return {key: parts[0] if len(parts) == 1 else {
        "kind": min((part["kind"] for part in parts), key=priority.__getitem__),
        "count": sum(part["count"] for part in parts),
        "revision": hashlib.sha256("".join(sorted(part["revision"] for part in parts)).encode()).hexdigest(),
    } for key, parts in grouped.items()}
