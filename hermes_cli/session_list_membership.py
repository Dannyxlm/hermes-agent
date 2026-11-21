"""Bounded, read-only provenance for client-cached session IDs (not access control)."""
import json
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from persistence.sqlite._holder import StoreHolder
from persistence.sqlite.conversation._store.compression import _CHAIN_CAP, _CHAIN_STEP_SQL
from persistence.sqlite.conversation._store.launches import thread_parents
from persistence.sqlite.conversation._store.sessions import _own_profile_name


def requested_ids(raw):
    """Validate without normalizing stored identifiers, including whitespace and Unicode."""
    try:
        ids = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail='ids must be a JSON array') from exc
    if (not isinstance(ids, list) or len(ids) > 100
            or any(not isinstance(sid, str) or not 1 <= len(sid) <= 256 for sid in ids)):
        raise HTTPException(status_code=400, detail='ids must contain at most 100 nonempty strings of at most 256 characters')
    try:
        for sid in ids:
            sid.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=400, detail='ids must be valid UTF-8 strings') from exc
    return list(dict.fromkeys(ids))


def trusted_thread_parents(db, ids):
    """Only self or SELECTED compression ancestors can supply launch evidence.

    Never project forward: a malformed descendant binding must not relabel its human
    ancestor. The selected-step predicate also rejects ordinary branch/reset children
    and unselected siblings. Cycles and depth-capped walks provide no evidence.
    """
    if not ids:
        return {}
    step = _CHAIN_STEP_SQL.replace('parent.id = ?', 'parent.id = p.id')
    rows = db._read_all(f"""
        WITH RECURSIVE ancestors(key,id,depth,seen,cycle) AS (
            SELECT requested.value,s.id,0,'/' || hex(s.id) || '/',0
            FROM json_each(?) requested CROSS JOIN sessions s ON s.id=requested.value
            UNION ALL
            SELECT a.key,p.id,a.depth+1,a.seen || hex(p.id) || '/',
                instr(a.seen,'/' || hex(p.id) || '/')>0
            FROM ancestors a CROSS JOIN sessions s ON s.id=a.id
            JOIN sessions p ON p.id=s.parent_session_id
            WHERE a.depth < ? AND NOT a.cycle AND s.id=({step})
        )
        SELECT key,id FROM ancestors
        WHERE key NOT IN (SELECT key FROM ancestors WHERE cycle OR depth>=?)
    """, (json.dumps(ids), _CHAIN_CAP, _CHAIN_CAP))
    lineages = {}
    for row in rows:
        lineages.setdefault(row['key'], []).append(row['id'])
    return thread_parents(db, _own_profile_name(db) or 'default', lineages)


def read_membership(db_path, ids):
    """Strict mode=ro: unlike general browse reads, never reconcile schema drift."""
    from hermes_cli.web_routers._common import corrupt_store_as_status

    with corrupt_store_as_status(db_path):
        try:
            with StoreHolder(Path(db_path), read_only=True) as db:
                parents = trusted_thread_parents(db, ids)
                return {'profile': _own_profile_name(db) or 'default',
                        'thread_session_ids': [sid for sid in ids if sid in parents]}
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail='Session store unavailable; membership is unknown') from exc
