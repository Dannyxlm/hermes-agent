"""Read-only Inbox projections; no runtime hydration or delivery enrollment."""
from pathlib import Path
import math
import re
import sys
from threading import Lock

from . import server_requests


def summary_scope(profile, *, replies_complete=None):
    server = sys.modules.get("tui_gateway.server")
    scope = {"profile": profile, "pending_scope": "process", "pending_complete": server is not None}
    if server is not None:
        scope["pending_epoch"] = server_requests.inbox_epoch()
    if replies_complete is not None:
        scope["replies_complete"] = replies_complete
    return scope


_WARNING_INTERVAL = 60

# Active-list requests can overlap on the dispatch pool.
_WARNING_LOCK = Lock()


def _warn_unavailable(session, error):
    import logging
    import time
    now = time.monotonic()
    with _WARNING_LOCK:
        if now - session.get('_inbox_warning_at', -_WARNING_INTERVAL) < _WARNING_INTERVAL:
            return
        session['_inbox_warning_at'] = now
    logging.getLogger(__name__).warning('Inbox run summary unavailable (%s)', type(error).__name__)


def _runs_readonly(path, keys):
    import sqlite3
    from hermes_state_holders import read_only_db_uri
    db = sqlite3.connect(read_only_db_uri(path), uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.row_factory = sqlite3.Row
        rows = db.execute(f"""SELECT scope,run_id,status,updated_at FROM (
            SELECT scope,run_id,status,updated_at,
                   ROW_NUMBER() OVER (PARTITION BY scope ORDER BY started_at DESC,rowid DESC) AS position
            FROM runs WHERE scope IN ({','.join('?' for _ in keys)})
        ) WHERE position=1""", keys).fetchall()
        return {row['scope']: {'run_id': row['run_id'], 'status': row['status'], 'at': row['updated_at']} for row in rows}
    finally:
        db.close()


def live_projections(entries, launch_home):
    """One metadata/reply read per profile and one run read for the whole list."""
    import sqlite3
    from hermes_constants import profile_name_for_home
    from hermes_state import SessionDB
    from .mobile_session_scope import projection_destinations

    result = {sid: {} for sid, _key, _session in entries}
    groups, scopes = {}, {}
    for sid, key, session in entries:
        # A fresh runtime has no durable destination yet. It is not an error.
        if not session.get('session_key') and not getattr(session.get('agent'), 'session_id', None):
            continue
        home = Path(session.get('profile_home') or launch_home)
        groups.setdefault(home, []).append((sid, key, session))
    for home, members in groups.items():
        path = home / 'state.db'
        if not path.is_file():
            continue
        try:
            with SessionDB(db_path=path, read_only=True) as db:
                destinations = projection_destinations(db, profile_name_for_home(home), list(dict.fromkeys(key for _, key, _ in members)))
                roots = {dest['root']['id']: dest['root'] for dest in destinations.values()}
                replies = page_replies(db, list(roots.values()))
            for sid, key, session in members:
                dest = destinations.get(key)
                if dest is None:
                    _warn_unavailable(session, ValueError('unknown stored session'))
                    continue
                result[sid]['title'] = dest['title']
                if reply := replies.get(dest['root']['id']):
                    result[sid]['last_assistant_reply'] = reply
                scopes[sid] = dest['scope'].key
        except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
            for _sid, _key, session in members:
                _warn_unavailable(session, error)
    path = Path(launch_home) / 'mobile-push' / 'outbox.sqlite3'
    if scopes and path.is_file():
        try:
            runs = _runs_readonly(path, list(set(scopes.values())))
            for sid, key in scopes.items():
                if key in runs:
                    result[sid]['latest_run'] = runs[key]
        except (OSError, sqlite3.Error) as error:
            for _sid, _key, session in entries:
                _warn_unavailable(session, error)
    return result


def live_items(snapshot, current_sid=""):
    from tui_gateway import server
    entries = [(sid, server._session_lookup_key(session, fallback=sid), session) for sid, session in snapshot]
    projections = live_projections(entries, server._hermes_home)
    return [server._session_live_item(sid, session, current_sid, projection=projections[sid])
            for sid, _, session in entries]


def latest_run(session, launch_home, stored_id):
    """Choose the canonical-bot or ordinary producer scope explicitly."""
    return live_projections([('', stored_id, session)], launch_home)[''].get('latest_run')


def page_replies(db, rows):
    """One indexed message query for this page's already-resolved compression chains."""
    lineages = {row["id"]: row.get("_lineage_ids") or [row["id"]] for row in rows}
    read_markers = {row["id"]: row.get("last_read_at") for row in rows}
    ids = list({sid for chain in lineages.values() for sid in chain})
    if not ids:
        return {}
    latest = db._read_all(f"""
        SELECT m.session_id, m.id, m.timestamp, substr(m.content, 1, 1200) AS head FROM messages m
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
            # The read marker is lineage-stamped by Desktop's existing PATCH.
            # NULL means never tracked/read, not historical unread debt. Compare
            # the final reply itself, rather than newer user/tool activity.
            read_at = read_markers[key]
            reply_at = final["timestamp"]
            unread = (isinstance(read_at, (int, float)) and math.isfinite(read_at) and read_at >= 0
                      and isinstance(reply_at, (int, float)) and math.isfinite(reply_at) and reply_at > read_at)
            reply = {"row_id": final["id"], "at": reply_at, "unread": unread}
            preview = reply_preview(final["head"])
            if preview:
                reply["preview"] = preview
            result[key] = reply
    return result


def live_reply(session, launch_home, stored_id):
    """Project the durable reply without hydrating a runtime or creating a store."""
    import logging
    import sqlite3
    from hermes_state import SessionDB

    path = Path(session.get("profile_home") or launch_home) / "state.db"
    if not path.is_file():
        return None
    try:
        with SessionDB(db_path=path, read_only=True) as db:
            chain = db.get_compression_lineage(stored_id)
            if not chain:
                return None
            root = db.get_session(chain[0])
            if root is None:
                return None
            root["_lineage_ids"] = chain
            return page_replies(db, [root]).get(root["id"])
    except (OSError, sqlite3.Error, RuntimeError) as error:
        logging.getLogger(__name__).warning("Inbox reply summary unavailable (%s)", type(error).__name__)
        return None


REPLY_PREVIEW_CHARS = 240


def reply_preview(text, limit=REPLY_PREVIEW_CHARS):
    """Plain one-paragraph glance at a reply: markdown markers and whitespace collapsed,
    cut at a word boundary. Bounded by the SQL head read; never the full message."""
    if not isinstance(text, str):
        return None

    body = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)  # drop fenced code
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", body)  # images
    body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)  # links -> text
    body = re.sub(r"(?m)^\s*MEDIA:[^\n]*$", "", body)
    lines = []
    for line in body.splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+", line)
        line = re.sub(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"[*`]{1,3}", "", line)
        # Paired emphasis/strike markers only: underscores within identifiers
        # and an unpaired home-directory tilde are ordinary text.
        line = re.sub(r"(?<!\w)_{1,3}(\S(?:.*?\S)?)_{1,3}(?!\w)", r"\1", line)
        line = re.sub(r"~~(\S(?:.*?\S)?)~~", r"\1", line)
        line = line.strip()
        if heading and line and line[-1] not in ".!?:;":
            line += "."
        lines.append(line)
    body = " ".join(" ".join(lines).split())
    if not body:
        return None
    if len(body) <= limit:
        return body
    cut = body[:limit].rsplit(" ", 1)[0].rstrip(",;:.-")
    return (cut or body[:limit]) + "…"


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
