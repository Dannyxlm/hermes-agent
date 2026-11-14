"""One-transaction retention batches; large conversations retain rows until their last chunk."""
from __future__ import annotations

import json

from hermes_state_common import _id_chunks, _non_continuation_child_sql, _placeholders, emit_session_deleted
from hermes_state_keys import prefix_range
from hermes_state_mutation_retirement import _LIVE_LEDGER_SQL, _local_conversations, retire_prunable, retired_in

PENDING_PREFIX = 'maintenance.prune.pending.v1.'


def conversation_group(conn, seeds):
    """Connected local conversations and compression continuations, using target/parent indexes."""
    found, todo = set(), set(seeds)
    while todo:
        sid = todo.pop()
        if sid in found:
            continue
        found.add(sid)
        local, _ = _local_conversations(conn, [sid])
        todo.update(set(local.get(sid, ())) - found)
        edge = " AND p.end_reason='compression' " + _non_continuation_child_sql('c.', 'p.id')
        rows = conn.execute(
            "SELECT c.id,p.id FROM sessions c JOIN sessions p ON p.id=c.parent_session_id WHERE c.id=?" + edge
            + " UNION SELECT c.id,p.id FROM sessions p JOIN sessions c ON c.parent_session_id=p.id WHERE p.id=?" + edge,
            (sid, sid)).fetchall()
        for child, parent in rows:
            todo.update({child, parent} - found)
    return found


def _pending_groups(conn):
    return {key: json.loads(raw) for key, raw in conn.execute(
        'SELECT key,value FROM state_meta WHERE key>=? AND key<?', prefix_range(PENDING_PREFIX))}


def remove_pending_members(conn, session_ids):
    """Alternate delete paths repair interrupted groups in the same transaction as their delete."""
    removed = set(session_ids)
    if not removed:
        return
    for key, ids in _pending_groups(conn).items():
        if removed.intersection(ids):
            remaining = [sid for sid in ids if sid not in removed]
            if remaining:
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(remaining), key))
            else:
                conn.execute('DELETE FROM state_meta WHERE key=?', (key,))


def pending_members(conn, ids):
    existing = {sid for sid in ids if conn.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone()}
    return existing if existing and all(retired_in(conn, sid) for sid in existing) else None


def _groups(conn, candidates):
    pending = {key: members for key, ids in _pending_groups(conn).items()
               if (members := pending_members(conn, ids)) is not None}
    by_sid = {sid: (key, ids) for key, ids in pending.items() for sid in ids}
    seen, groups, skipped = set(), [], set()
    for sid in sorted(candidates):
        if sid in seen:
            continue
        key, ids = by_sid.get(sid, (None, None))
        group = set(ids) if ids is not None else conversation_group(conn, [sid])
        seen.update(group)
        if not group <= candidates:
            skipped.update(group & candidates)
        else:
            groups.append((key, sorted(group)))
    return groups, skipped


def _eligible_group(db, conn, ids, where, params, *, include_pinned, guards):
    rows = [row for chunk in _id_chunks(ids) for row in conn.execute(
        f'SELECT s.id,s.pinned FROM sessions s WHERE s.id IN ({_placeholders(chunk)}) AND {where}',
        (*chunk, *params))]
    if len(rows) != len(ids) or (not include_pinned and any(row['pinned'] for row in rows)):
        return False
    if guards and db._guarded_ids(conn, ids):
        return False
    return not any(conn.execute(_LIVE_LEDGER_SQL, (sid, sid)).fetchone() for sid in ids)


def _message_ids(conn, ids, limit):
    # LIMIT is applied per session as well: never materialize a large group's transcript ids.
    result = []
    for sid in ids:
        result.extend(row[0] for row in conn.execute(
            'SELECT id FROM messages WHERE session_id=? ORDER BY id LIMIT ?', (sid, limit - len(result))))
        if len(result) == limit:
            break
    return result


def _delete_rows(conn, ids):
    for chunk in _id_chunks(ids):
        ph = _placeholders(chunk)
        conn.execute(f'UPDATE sessions SET parent_session_id=NULL WHERE parent_session_id IN ({ph})', chunk)
        conn.execute(f'DELETE FROM sessions WHERE id IN ({ph})', chunk)


def _write_batch(db, conn, groups, snapshot, cap, where, params, include_pinned, guards):
    from hermes_state_media import retire_spillover
    deleted, skipped, in_progress, messages = [], [], [], 0
    for key, ids in groups:
        if not _eligible_group(db, conn, ids, where, params, include_pinned=include_pinned, guards=guards):
            skipped.extend(ids)
            continue
        # Re-read membership under the writer lock, so a reset/compression after selection cannot
        # strand a newly published segment. Pending groups keep their original durable membership.
        if key:
            saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
            if not saved or pending_members(conn, json.loads(saved[0])) != set(ids):
                skipped.extend(ids)
                continue
        elif conversation_group(conn, ids) != set(ids):
            skipped.extend(ids)
            continue
        budget = cap - messages
        chunk = _message_ids(conn, ids, budget + 1)
        oversize = len(chunk) > budget
        if oversize and (messages or deleted):
            break
        if key is None:
            quiet = retire_prunable(conn, ids, snapshot=snapshot, capture_spillover=False)
            if set(quiet) != set(ids):
                raise RuntimeError('prune group changed while holding the write lock')
            if oversize:
                key = PENDING_PREFIX + ids[0]
                conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, json.dumps(ids)))
        chunk = chunk[:budget]
        retire_spillover(conn, message_ids=chunk)
        for part in _id_chunks(chunk):
            conn.execute(f'DELETE FROM messages WHERE id IN ({_placeholders(part)})', part)
        messages += len(chunk)
        if oversize:
            in_progress.extend(ids)
            break
        _delete_rows(conn, ids)
        if key:
            conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
        deleted.extend(ids)
        if messages == cap:
            break
    return deleted, skipped, in_progress, messages


def prune_batch(db, older_than_days, source, sessions_dir, guards, session_ids, message_cap, filters):
    """Return progress for exactly one write transaction. Cleanup is explicitly caller-owned."""
    if isinstance(message_cap, bool) or not isinstance(message_cap, int) or message_cap <= 0:
        raise ValueError('message_cap must be a positive integer')
    if filters.get('include_open'):
        raise ValueError('prune_sessions() never deletes open sessions: include_open is archive-only')
    requested = None if session_ids is None else set(session_ids)
    where, params = db._prune_where(older_than_days, source, filters, whole_lineages=True)
    with db._read_ctx() as conn:
        candidates = {r[0] for r in conn.execute(f'SELECT s.id FROM sessions s WHERE {where}', params)}
        if requested is not None:
            candidates &= requested
        groups, skipped = _groups(conn, candidates)
        if not groups:
            skipped.update(requested or ())
            return {'deleted': 0, 'deleted_ids': [], 'skipped_ids': sorted(skipped),
                    'messages_deleted': 0, 'in_progress_ids': [], 'remaining': 0, 'has_more': False}
        from hermes_state_prune_snapshot import PruneSnapshot, receipts_version
        # New/repointed receipts invalidate the projection; recheck once after taking the lock.
        version = receipts_version(conn)
        snapshot = PruneSnapshot(conn, candidates)
    check_filters = {**filters, 'include_pinned': True}
    check_where, check_params = db._prune_where(older_than_days, source, check_filters)
    # The reader may borrow the writer connection under the non-reentrant store lock.
    # Exit its context before entering _execute_write; the version check still fences races.
    result = db._execute_write(lambda writer: _write_batch(
        db, writer, groups, snapshot, message_cap, check_where, check_params,
        bool(filters.get('include_pinned')), guards) if receipts_version(writer) == version else None)
    if requested is not None:
        skipped |= requested - candidates
    # A concurrent receipt change invalidates the snapshot (including newly inserted receipts).
    # Yield to the owner between attempts so drain can interrupt even under continual writes.
    deleted, rejected, pending, messages = result if result is not None else ([], [], [], 0)
    skipped.update(rejected)
    emit_session_deleted(db, deleted, reason='prune', sessions_dir=sessions_dir)
    for sid in deleted:
        db._remove_session_files(sessions_dir, sid)
    remaining = candidates - skipped - set(deleted)
    return {'deleted': len(deleted), 'deleted_ids': deleted, 'skipped_ids': sorted(skipped),
            'messages_deleted': messages, 'in_progress_ids': pending,
            'remaining': len(remaining), 'has_more': bool(remaining)}


def recheck_unbounded(db, conn, candidates, where, params, *, include_pinned, guards):
    """Legacy all-at-once prune revalidates only its reader-selected rows under the writer lock."""
    ids = {sid for sid in candidates if _eligible_group(
        db, conn, [sid], where, params, include_pinned=include_pinned, guards=guards)}
    if not include_pinned:
        # A newly pinned compression ancestor also protects its tail. Start every walk at a
        # primary key, unlike the broad listing predicate's all-session recursive seed.
        edge = _non_continuation_child_sql('c.', 'p.id')
        query = ("WITH RECURSIVE ancestors(id,pinned) AS (SELECT id,pinned FROM sessions WHERE id=? UNION "
                 "SELECT p.id,p.pinned FROM ancestors a JOIN sessions c ON c.id=a.id "
                 "JOIN sessions p ON p.id=c.parent_session_id WHERE p.end_reason='compression' "
                 + edge + ") SELECT 1 FROM ancestors WHERE pinned=1 LIMIT 1")
        ids = {sid for sid in ids if not conn.execute(query, (sid,)).fetchone()}
    edge = _non_continuation_child_sql('c.', 'p.id')
    children = ("SELECT c.id FROM sessions p JOIN sessions c ON c.parent_session_id=p.id "
                "WHERE p.id=? AND p.end_reason='compression' " + edge)
    kept = {sid for sid in ids if any(row[0] not in ids for row in conn.execute(children, (sid,)))}
    todo = list(kept)
    while todo:
        sid = todo.pop()
        row = conn.execute("SELECT p.id FROM sessions c JOIN sessions p ON p.id=c.parent_session_id "
                           "WHERE c.id=? AND p.end_reason='compression' " + edge, (sid,)).fetchone()
        if row and row[0] in ids and row[0] not in kept:
            kept.add(row[0])
            todo.append(row[0])
    return sorted(ids - kept)


def finish_prune(db, prompt_batch_size=100):
    """Global work once per owner loop, outside transcript transactions; safe to retry after a crash."""
    from hermes_state_media import collect_retired_media
    from hermes_state_mutation_retirement import prune_retired_tombstones
    if prompt_batch_size <= 0:
        raise ValueError('prompt_batch_size must be positive')
    unreferenced = ('NOT EXISTS (SELECT 1 FROM sessions WHERE sessions.system_prompt_hash=system_prompts.hash) '
                    'AND NOT EXISTS (SELECT 1 FROM sessions WHERE sessions.tool_names=system_prompts.hash)')
    hashes = [row[0] for row in db._read_all(f'SELECT hash FROM system_prompts WHERE {unreferenced}')]
    count = 0
    for chunk in _id_chunks(hashes, size=prompt_batch_size):
        count += db._execute_write(lambda conn: conn.execute(
            f'DELETE FROM system_prompts WHERE hash IN ({_placeholders(chunk)}) AND {unreferenced}', chunk).rowcount)
    db._execute_write(prune_retired_tombstones)
    collect_retired_media(db)
    return {'system_prompts_deleted': count}
