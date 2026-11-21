"""Bounded retention orchestration shared by the owner and the offline CLI."""
from __future__ import annotations

import asyncio
import time

from hermes_state_common import _COMPRESSION_LOCK_ROW_SQL, _sql_session_last_active
from hermes_state_maintenance import _not_pinned_sql
from hermes_state_messages import _TURN_LEASE_ROW_SQL, _stale_holder
from hermes_state_prune import _eligible_group, _groups

# Provisional production-shaped benchmark defaults; writers get a turn between commits.
PRUNE_MESSAGE_CAP = 200
PRUNE_BATCH_PAUSE = 0.05


def guarded_readonly(db, conn, sid):
    """The destructive guard's liveness decision without reclaiming expired/dead leases."""
    conversation = db._session_turn_lease_key_on_conn(conn, sid)
    rows = (conn.execute(_COMPRESSION_LOCK_ROW_SQL, (sid,)).fetchone(),
            conn.execute(_TURN_LEASE_ROW_SQL, (conversation,)).fetchone())
    now = time.time()
    return any(row is not None and not _stale_holder(row, now) for row in rows)


def prune_preview(db, filters, session_ids=None):
    """Use the batch selector's whole-group and busy checks, on a read connection only."""
    scope = dict(filters)
    days, source = scope.pop('older_than_days', None), scope.pop('source', None)
    where, params = db._prune_where(days, source, scope, whole_lineages=True)
    requested = None if session_ids is None else set(session_ids)
    check_where, check_params = db._prune_where(days, source, {**scope, 'include_pinned': True})
    with db._read_ctx() as conn:
        rows = [dict(row) for row in conn.execute(
            f'SELECT s.*, {_sql_session_last_active("s")} AS last_active FROM sessions s WHERE {where} '
            'ORDER BY last_active,s.started_at,s.id', params)]
        candidates = {row['id'] for row in rows}
        if requested is not None:
            candidates &= requested
        groups, skipped = _groups(conn, candidates)
        accepted = set()
        for _, ids in groups:
            if (_eligible_group(db, conn, ids, check_where, check_params,
                                include_pinned=bool(scope.get('include_pinned')), guards=False)
                    and not any(guarded_readonly(db, conn, sid) for sid in ids)):
                accepted.update(ids)
            else:
                skipped.update(ids)
        if requested is not None:
            skipped.update(requested - accepted)
        return {'candidates': [row for row in rows if row['id'] in accepted],
                'skipped_ids': sorted(skipped), 'skipped': len(skipped)}


def sweep_preview(db, source, idle_seconds):
    cutoff = time.time() - idle_seconds
    with db._read_ctx() as conn:
        rows = conn.execute(
            f'SELECT id FROM sessions WHERE ended_at IS NULL AND source=? AND {_not_pinned_sql("sessions")} '
            f'AND started_at < ? AND {_sql_session_last_active("sessions")} < ? ORDER BY id',
            (source, cutoff, cutoff))
        return [row[0] for row in rows if not guarded_readonly(db, conn, row[0])]


async def run_prune_batches(db, filters, *, sessions_dir, session_ids=None,
                            sweep_idle_seconds=None, stopping=lambda: False, after_batch=None):
    """Yield between commits; a disconnect is shielded by the owner, a drain stops between batches."""
    filters = dict(filters)
    # Direct RPC callers can still use the legacy relative cutoff. Freeze it once for this run.
    days = filters.pop('older_than_days', None)
    if days is not None and filters.get('last_active_before') is None and filters.get('started_before') is None:
        filters['last_active_before'] = time.time() - days * 86400
    sweep_only = (sweep_idle_seconds is not None and session_ids is None and days is None
                  and not any(filters.get(key) is not None for key in
                              ('last_active_before', 'last_active_after', 'started_before', 'started_after')))
    remaining_ids = set() if sweep_only else (None if session_ids is None else set(session_ids))
    deleted, skipped, pending, closed = set(), set(), set(), []
    messages, remaining, more = 0, 0, True
    if sweep_idle_seconds is not None and not stopping():
        # Even an all-ages prune must never delete a row it just closed for recovery.
        preview = await asyncio.to_thread(prune_preview, db, filters, [] if sweep_only else session_ids)
        remaining_ids = {row['id'] for row in preview['candidates']}
        skipped.update(preview['skipped_ids'])
        closed = await asyncio.to_thread(
            db.sweep_orphaned_sessions, max_idle_seconds=sweep_idle_seconds,
            sources=(filters['source'],), exclude_pinned=True, respect_gateway_heartbeats=False)
    while more and not stopping():
        batch = await asyncio.to_thread(
            db.prune_sessions, older_than_days=None, sessions_dir=sessions_dir,
            session_ids=None if remaining_ids is None else sorted(remaining_ids),
            message_cap=PRUNE_MESSAGE_CAP, exclude_active_write_guards=True, **filters)
        deleted.update(batch['deleted_ids'])
        skipped.update(batch['skipped_ids'])
        pending.update(batch['in_progress_ids'])
        pending.difference_update(deleted)
        messages += batch['messages_deleted']
        remaining, more = batch['remaining'], batch['has_more']
        if remaining_ids is not None:
            remaining_ids.difference_update(deleted | skipped)
        if after_batch is not None:
            after_batch(sorted(set(batch['deleted_ids']) | set(batch['in_progress_ids'])))
        if more and not stopping():
            await asyncio.sleep(PRUNE_BATCH_PAUSE)
    interrupted = bool(stopping())
    if not interrupted:
        await asyncio.to_thread(db.finish_prune_sessions, prompt_batch_size=100)
    return {'deleted': len(deleted), 'deleted_ids': sorted(deleted), 'skipped': len(skipped),
            'skipped_ids': sorted(skipped), 'messages_deleted': messages, 'in_progress_ids': sorted(pending),
            'remaining': remaining, 'has_more': more, 'interrupted': interrupted,
            'closed': len(closed), 'closed_ids': sorted(closed)}
