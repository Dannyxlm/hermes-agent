"""``session.prune``: the owner runs `hermes sessions prune` for its own store.

The CLI computes the filters and shows the preview from a read-only view; the delete itself is
this owner's write, through the same ``prune_sessions`` / ``retire_prunable`` path housekeeping
uses (busy sessions and the logical owner of a retained reset child are skipped). A CLI writing
the store under a live gateway would be a second writer, so it never does.
"""
import asyncio
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError

_SCALARS = (type(None), bool, int, float, str)


def _filters(raw):
    from hermes_state_maintenance import _PRUNE_FILTER_NAMES
    if (not isinstance(raw, dict) or set(raw) - (_PRUNE_FILTER_NAMES | {'older_than_days'})
            or not all(isinstance(value, _SCALARS) for value in raw.values())):
        raise RuntimeStoreError('invalid_params')
    days = raw.get('older_than_days')
    if days is not None and (isinstance(days, bool) or not isinstance(days, (int, float))
                             or not 0 <= days < float('inf')):
        raise RuntimeStoreError('invalid_params')
    return dict(raw)


async def prune(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    # Store-wide deletion across every principal's rows: the local operator only (a ticket from
    # this host's private control socket), never a remote or browser grant.
    if not connection.native_owner or 'session:operator' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    never_active = params.get('never_active_days')
    if ('filters' in params) == (never_active is not None) or (
            never_active is not None and (isinstance(never_active, bool) or not isinstance(never_active, (int, float))
                                          or never_active < 0)):
        raise RuntimeStoreError('invalid_params')
    filters = _filters(params['filters']) if 'filters' in params else None
    session_ids = params.get('session_ids')
    sweep = params.get('sweep_idle_seconds')
    if (session_ids is not None and (not isinstance(session_ids, list)
            or any(not isinstance(sid, str) or not sid for sid in session_ids))):
        raise RuntimeStoreError('invalid_params')
    if sweep is not None and (filters is None or not filters.get('source')
            or isinstance(sweep, bool) or not isinstance(sweep, (int, float))
            or not 0 < sweep < float('inf')):
        raise RuntimeStoreError('invalid_params')
    if filters is None and session_ids is not None:
        raise RuntimeStoreError('invalid_params')
    authority._require_admission_open()
    from gateway.session_runtime_workers import track_mutation
    task = track_mutation(authority, _apply(authority, filters, never_active, session_ids, sweep))
    return await asyncio.shield(task)


async def _apply(authority, filters, never_active, session_ids=None, sweep_idle_seconds=None):
    from hermes_constants import get_hermes_home
    from gateway.session_mutations import retire_live_sessions
    # Same transcript dir the CLI's local prune unlinks under (dispatch runs in the owner's scope).
    sessions_dir = Path(get_hermes_home()) / 'sessions'
    deleted_ids = []
    if filters is None:
        deleted, routing, skipped = await asyncio.to_thread(
            authority.db.prune_never_active_keyed_sessions, older_than_days=float(never_active),
            sessions_dir=sessions_dir, deleted_ids=deleted_ids)
        result = {'deleted': deleted, 'routing_deleted': routing, 'skipped': skipped}
    else:
        from hermes_state_prune_runner import run_prune_batches
        return await run_prune_batches(
            authority.db, filters, sessions_dir=sessions_dir, session_ids=session_ids,
            sweep_idle_seconds=sweep_idle_seconds,
            stopping=lambda: authority.runner._draining,
            after_batch=lambda ids: retire_live_sessions(authority, ids))
    retire_live_sessions(authority, deleted_ids)
    return result
