"""Bounded, read-only Inbox pages under an explicit widget profile grant."""

import base64
import hashlib
import json
import math
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from .inbox_summaries import page_attention, summary_scope


@dataclass
class _Projection:
    signature: tuple
    rows: list
    replies: dict
    attention: dict | None = None
    items: list | None = None
    revision: str | None = None


# Process-local, bounded by served stores; never retains a connection or grant.
_PROJECTIONS = OrderedDict()
_PROJECTION_LOCK = Lock()


class WidgetInboxChanged(RuntimeError):
    """The client must keep its last good projection and retry from page one."""


def profile_home(profile):
    from hermes_cli.profiles import validate_profile_name
    from . import server
    validate_profile_name(profile)
    home = Path(server._profile_home(profile) or server._hermes_home).resolve()
    if not (home / "state.db").is_file():
        raise FileNotFoundError("profile state unavailable")
    return home


def _revision(items):
    # Bind every page to the ordered Inbox projection, not unrelated DB/WAL writes.
    return hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cursor_offset(cursor, grant, revision, epoch):
    if cursor is None:
        return 0
    try:
        if len(cursor) > 1024:
            raise ValueError("invalid cursor")
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        offset = value["offset"]
        if type(offset) is not int or offset < 0 or set(value) != {"offset", "grant", "revision", "epoch"}:
            raise ValueError("invalid cursor")
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("invalid cursor") from error
    if (value["grant"], value["revision"], value["epoch"]) != (grant, revision, epoch):
        raise WidgetInboxChanged("Inbox changed between pages")
    return offset


def _signature(db):
    from hermes_state_sessions import _session_filter_where
    from hermes_state_common import _SQL_IN_WINDOW, _sql_json_extract

    clauses, params = _session_filter_where(exclude_children=True)
    clauses.append("s.hidden = 0")
    # Metadata and indexed timestamp/reply-head seeks, never previews or reply grouping.
    # Include linked segments (even hidden ones) because reply/local-lineage
    # projection can depend on them. UNION also bounds malformed cycles.
    rows = db._conn.execute(f"""
        WITH RECURSIVE members(id) AS (
            SELECT s.id FROM sessions s WHERE {' AND '.join(clauses)}
            UNION
            SELECT s.id FROM sessions s JOIN members m ON s.parent_session_id = m.id
            WHERE {_sql_json_extract('s.model_config', '$._delegate_from')} IS NULL
              AND COALESCE(s.source, '') != 'tool'
            UNION
            SELECT s.parent_session_id FROM sessions s JOIN members m ON s.id = m.id
            WHERE s.parent_session_id IS NOT NULL
        )
        SELECT s.id, s.parent_session_id, s.source, s.session_key, s.user_id,
               s.model_config, s.started_at, s.ended_at, s.end_reason, s.title,
               s.archived, s.hidden, s.last_read_at, s.last_activity_at,
               s.message_count, s.transcript_revision,
               (SELECT MAX(m.timestamp) FROM messages m INDEXED BY idx_messages_session
                WHERE m.session_id = s.id AND m.timestamp {_SQL_IN_WINDOW}),
               reply.id, reply.timestamp
        FROM sessions s JOIN members m ON m.id = s.id
        LEFT JOIN messages reply ON reply.id = (
            SELECT r.id FROM messages r WHERE r.session_id = s.id AND r.role = 'assistant'
              AND (r.active = 1 OR r.compacted = 1)
              AND COALESCE(r.finish_reason, '') != 'tool_calls'
              AND length(trim(COALESCE(r.content, ''), char(9)||char(10)||char(13)||' ')) > 0
            ORDER BY r.id DESC LIMIT 1)
        ORDER BY s.id
    """, params).fetchall()
    return tuple(tuple(row) for row in rows)


def _replies(rows, signature):
    latest = {r[0]: (r[-2], r[-1]) for r in signature if r[-2] is not None}
    result = {}
    for row in rows:
        candidates = [latest[sid] for sid in row.get('_lineage_ids', [row['id']]) if sid in latest]
        if candidates:
            ident, at = max(candidates)
            read = row.get('last_read_at')
            unread = (isinstance(read, (int, float)) and math.isfinite(read) and read >= 0
                      and isinstance(at, (int, float)) and math.isfinite(at) and at > read)
            result[row['id']] = {'row_id': ident, 'at': at, 'unread': unread}
    return result


def _projection(db, home):
    with _PROJECTION_LOCK:
        signature = _signature(db)
        cut = _PROJECTIONS.get(home)
        if cut is None or cut.signature != signature:
            rows = db.list_sessions_rich(limit=-1, compact_rows=True,
                order_by_last_active=True, include_pinned=False)
            cut = _Projection(signature, rows, _replies(rows, signature))
        attention = page_attention(cut.rows, home)
        if cut.items is None or cut.attention != attention:
            cut.items = _items(cut.rows, cut.replies, attention)
            cut.revision = _revision(cut.items)
            cut.attention = attention
        _PROJECTIONS[home] = cut
        _PROJECTIONS.move_to_end(home)
        while len(_PROJECTIONS) > 4:
            _PROJECTIONS.popitem(last=False)
        return cut.items, cut.revision


def _items(rows, replies, attention):
    items = []
    for row in rows:
        sid = row['id']
        items.append({"session_id": row.get('_lineage_root_id') or sid,
                      "title": row.get('title') or "Chat", "source": row.get('source'),
                      "archived": bool(row.get('archived')), "hidden": bool(row.get('hidden')),
                      "last_read_at": row.get('last_read_at'),
                      "reply": {k: v for k, v in replies.get(sid, {}).items() if k != 'preview'} or None,
                      "attention": attention.get(sid)})
    return items


def inbox_snapshot(store, token, cursor=None):
    from hermes_state import SessionDB
    from . import server
    grant = store.widget_inbox_grant(token)
    home = profile_home(grant['profile'])
    if str(home) != grant['home']:
        raise PermissionError("profile grant changed")
    path = home / "state.db"
    coverage = summary_scope(grant['profile'], replies_complete=True)
    epoch = coverage.get('pending_epoch')
    with server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False):
        with SessionDB(db_path=path, read_only=True) as db:
            # One read transaction covers membership, order, lineage and replies.
            db._conn.execute("BEGIN")
            items, revision = _projection(db, home)
    if summary_scope(grant['profile']).get('pending_epoch') != epoch:
        raise WidgetInboxChanged("Inbox changed while reading")
    # Revocation/profile switches during projection assembly must win.
    if store.widget_inbox_grant(token) != grant:
        raise WidgetInboxChanged("widget grant changed")
    offset = _cursor_offset(cursor, grant['grant_id'], revision, epoch)
    more = len(items) > offset + 100
    items = items[offset:offset + 100]
    next_cursor = base64.urlsafe_b64encode(json.dumps({"offset": offset + len(items),
        "grant": grant['grant_id'], "revision": revision, "epoch": epoch}).encode()).decode() if more else None
    return {"protocol_version": 2, "installation_id": grant['installation_id'],
            "connection_id": grant['connection_id'], "profile": grant['profile'],
            "grant_id": grant['grant_id'], "revision": revision,
            "generated_at": store.clock(), "offset": offset, "next_cursor": next_cursor,
            "inbox_summary_scope": coverage, "items": items}
