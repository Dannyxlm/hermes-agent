"""Stored ordinary-session destinations; canonical Bot Chat authority stays separate."""
from pathlib import Path

from hermes_state import SessionDB
from .mobile_push_payloads import Scope


def projection_destinations(db, profile, stored_ids):
    """Resolve a page using the same selected continuation step as SessionDB.

    Only metadata is read; branch/reset/delegate children never join the path.
    The input title is kept separately from the root read marker and tip title.
    """
    from hermes_state_compression import _CHAIN_CAP, _CHAIN_STEP_SQL
    from hermes_state_common import _RESET_CHILD_SQL, _sql_json_extract

    if not stored_ids:
        return {}
    fork = f"""COALESCE({_sql_json_extract('s.model_config', '$._branched_from')}, '') = s.parent_session_id
        OR COALESCE({_sql_json_extract('s.model_config', '$._delegate_from')}, '') = s.parent_session_id
        OR ({_RESET_CHILD_SQL.format(a='s')}) OR COALESCE(s.source, '') = 'tool'"""
    step = _CHAIN_STEP_SQL.replace('parent.id = ?', 'parent.id = c.id')
    rows = db._read_all(f"""
        WITH RECURSIVE requested(key) AS (VALUES {','.join('(?)' for _ in stored_ids)}),
        ancestors(key,id,depth) AS (
            SELECT key,s.id,0 FROM requested JOIN sessions s ON s.id=key
            UNION ALL
            SELECT a.key,p.id,a.depth+1 FROM ancestors a
            JOIN sessions s ON s.id=a.id JOIN sessions p ON p.id=s.parent_session_id
            WHERE p.end_reason='compression' AND NOT ({fork}) AND a.depth < {_CHAIN_CAP}
        ), roots(key,id) AS (
            SELECT key,id FROM ancestors a WHERE depth=(SELECT MAX(depth) FROM ancestors b WHERE b.key=a.key)
        ), chain(key,id,depth) AS (
            SELECT key,id,0 FROM roots
            UNION ALL
            SELECT c.key,child.id,c.depth+1 FROM chain c
            JOIN sessions child ON child.id=({step}) WHERE c.depth < {_CHAIN_CAP}
        )
        SELECT c.key,c.depth,s.id,s.title,s.last_read_at,
               original.title AS input_title FROM chain c JOIN sessions s ON s.id=c.id
        JOIN sessions original ON original.id=c.key ORDER BY c.key,c.depth
    """, list(stored_ids))
    grouped = {}
    for row in rows:
        if row['depth'] >= _CHAIN_CAP:
            raise RuntimeError('Compression lineage exceeds the safe depth limit')
        grouped.setdefault(row['key'], []).append(row)
    result = {}
    for key, chain in grouped.items():
        root, tip = chain[0], chain[-1]
        canonical = root['title'] == SessionDB.CANONICAL_BOT_CHAT_TITLE
        # Canonical Bot Chats have their own producer surface, not native_session.
        scope = Scope('native' if canonical else 'native_session', profile, root['id'])
        result[key] = {'scope': scope, 'tip': tip['id'], 'title': root['input_title'],
                       'root': {'id': root['id'], 'last_read_at': root['last_read_at'],
                                '_lineage_ids': [row['id'] for row in chain]}}
    return result


def resolve_destination(profile, home, stored_id):
    """Exact ID only, profile DB only, compression continuations only (never branches)."""
    path = Path(home) / "state.db"
    if not path.is_file():
        raise FileNotFoundError("profile state unavailable")
    with SessionDB(db_path=path, read_only=True) as db:
        row = db.get_session(stored_id)
        if not row:
            raise ValueError("unknown stored session")
        chain = db.get_compression_chain(stored_id)
        if not chain:
            raise ValueError("unknown lineage")
        root, tip = db.get_session(chain[0]), db.get_session(chain[-1])
        if not root or not tip or any(r.get("title") == SessionDB.CANONICAL_BOT_CHAT_TITLE for r in (root, tip)):
            raise ValueError("Bot Chat requires canonical registration")
        return Scope("native_session", profile, root["id"]), tip["id"], tip.get("title") or "Hermex Ava"
