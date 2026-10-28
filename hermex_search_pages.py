"""Bounded canonical search pages for Hermex, independent of legacy ranked search."""
from __future__ import annotations

import sqlite3
import time


def start_position(chain, high_water):
    """Global search walks newest-first from just above the high-water mark; a chat walks oldest-first."""
    return 0 if chain is not None else high_water + 1


def candidates(db, conn, query, chain, position, high_water, *, deadline, cancelled, batch=64):
    """Return candidate hits and the last examined id, even on an empty scan page.

    ``position`` is an exclusive bound in scan order: global search reads ids below it
    (newest first, so recent chats answer the first page), a chat reads ids above it.
    Global FTS uses its rowid seek, without snippet/rank or a store-wide sort.
    Per-chat scans use the session/id index and read at most one small batch.
    """
    newest_first = chain is None
    done = 0 if newest_first else high_water
    if newest_first and db._fts_enabled and not db._fts_stale and not db._contains_cjk(query):
        sql = """SELECT m.id, m.session_id, m.role FROM messages_fts f
                 JOIN messages m ON m.id=f.rowid
                 WHERE messages_fts MATCH ? AND f.rowid < ? AND f.rowid <= ?
                   AND m.role IN ('user','assistant') AND (m.active=1 OR m.compacted=1)
                   AND COALESCE(m.display_kind,'') <> 'hidden'
                 ORDER BY f.rowid DESC LIMIT ?"""
        try:
            fts_deadline = min(deadline, time.monotonic() + 0.25)
            conn.set_progress_handler(lambda: cancelled.is_set() or time.monotonic() >= fts_deadline, 1000)
            rows = conn.execute(sql, (query, position, high_water, batch)).fetchall()
            return rows, rows[-1]["id"] if rows else done, len(rows) < batch
        except sqlite3.OperationalError as exc:
            if str(exc) == "interrupted" and cancelled.is_set():
                raise
            # Syntax/tokenizer failures use the same bounded canonical fallback.
        finally:
            conn.set_progress_handler(lambda: cancelled.is_set() or time.monotonic() >= deadline, 1000)
    binds = []
    where = ""
    if chain is not None:
        where = "session_id IN (" + ",".join("?" for _ in chain) + ") AND "
        binds.extend(chain)
    scan = "id < ? AND id <= ? ORDER BY id DESC LIMIT ?" if newest_first else "id > ? AND id <= ? ORDER BY id LIMIT ?"
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE " + where + scan, (*binds, position, high_water, batch))]
    if not ids:
        return [], done, True
    predicate, values, _ = db._compile_like_boolean_query(query)
    rows = conn.execute(
        "SELECT m.id, m.session_id, m.role FROM messages m WHERE m.id IN ("
        + ",".join("?" for _ in ids) + ") AND m.role IN ('user','assistant') "
        "AND (m.active=1 OR m.compacted=1) AND COALESCE(m.display_kind,'') <> 'hidden' AND ("
        + (predicate or "0") + ") ORDER BY m.id" + (" DESC" if newest_first else ""), (*ids, *values)).fetchall()
    return rows, ids[-1], len(ids) < batch
