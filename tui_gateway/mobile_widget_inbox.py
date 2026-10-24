"""Bounded, read-only Inbox pages under an explicit widget profile grant."""

import base64
import hashlib
import json
from pathlib import Path

from .inbox_summaries import page_attention, page_replies, summary_scope


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
            rows = db.list_sessions_rich(limit=-1, compact_rows=True,
                order_by_last_active=True, include_pinned=False)
            replies = page_replies(db, rows)
            attention = page_attention(rows, home)
    items = []
    for row in rows:
        sid = row['id']
        items.append({"session_id": row.get('_lineage_root_id') or sid,
                      "title": row.get('title') or "Chat", "source": row.get('source'),
                      "archived": bool(row.get('archived')), "hidden": bool(row.get('hidden')),
                      "last_read_at": row.get('last_read_at'),
                      "reply": {k: v for k, v in replies.get(sid, {}).items() if k != 'preview'} or None,
                      "attention": attention.get(sid)})
    revision = _revision(items)
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
