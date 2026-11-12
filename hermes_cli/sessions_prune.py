"""CLI retention preview and exact-set submission; the owner and offline path share one loop."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

from hermes_cli.session_filters import build_prune_filters, describe_filters, format_epoch, parse_duration_seconds
from hermes_cli.sessions_owner import PRUNE_RPC_TIMEOUT
from hermes_state_prune_runner import prune_preview, run_prune_batches, sweep_preview


def _options(args):
    from hermes_cli.sessions_cmd import _any_filter_args
    if not _any_filter_args(args) and getattr(args, 'ids_file', None) is None:
        args.older_than = '90'
    filters = build_prune_filters(args)
    filters.update(archived=None if getattr(args, 'include_archived', False) else False,
                   include_pinned=getattr(args, 'include_pinned', False))
    ids_path = getattr(args, 'ids_file', None)
    # IDs are literal, not unique prefixes; only line separators are removed.
    ids = None if ids_path is None else sorted(set(
        line for line in Path(ids_path).read_text(encoding='utf-8-sig').splitlines() if line))
    timeout = getattr(args, 'timeout', PRUNE_RPC_TIMEOUT)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('--timeout must be a positive finite number of seconds')
    sweep = getattr(args, 'sweep_stale_open', False)
    idle = getattr(args, 'idle_threshold', None)
    if idle is not None and not sweep:
        raise ValueError('--idle-threshold requires --sweep-stale-open')
    seconds = None
    if sweep:
        if not filters.get('source') or idle is None:
            raise ValueError('--sweep-stale-open requires explicit --source and --idle-threshold')
        seconds = parse_duration_seconds(idle)
        if seconds is None or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('--idle-threshold must be a positive duration (e.g. 30d)')
    return filters, ids, timeout, seconds


def _preview(db, filters, ids, sweep):
    result = (prune_preview(db, filters, ids) if db is not None else
              {'candidates': [], 'skipped_ids': ids or [], 'skipped': len(ids or [])})
    closed = [] if sweep is None or db is None else sweep_preview(db, filters['source'], sweep)
    result.update(closed_ids=closed, closed=len(closed), deleted=0, deleted_ids=[], dry_run=True)
    # Never expose arbitrary session columns as the machine-readable retention contract.
    keys = ('id', 'source', 'ended_at', 'pinned', 'message_count', 'last_active', 'title', 'model')
    result['candidates'] = [{key: row.get(key) for key in keys} for row in result['candidates']]
    return result


def _print_preview(preview, filters, *, dry_run):
    rows = preview['candidates']
    print(f"{len(rows)} session(s) match ({describe_filters(filters)}):")
    for row in rows if dry_run else rows[:15]:
        print(f"  {row['id']}  {format_epoch(row.get('last_active')):<17} {row['source']:<10} "
              f"{row['message_count']:>4} msgs  {(row.get('title') or '')[:36]}")
    if preview['skipped']:
        print(f"Skipped {preview['skipped']} session(s): {', '.join(preview['skipped_ids'])}")
    if preview['closed']:
        print(f"Would close {preview['closed']} stale open session(s) (transcripts kept).")
    if dry_run:
        print('Dry run — nothing deleted or closed.')


def cmd_prune(db, args):
    from hermes_cli.sessions_cmd import _confirm_or_exit_code, _sessions_dir
    from hermes_cli.gateway_client import GatewayClientError
    from hermes_cli.sessions_owner import owner_prune, report_owner_failure
    try:
        filters, ids, timeout, sweep = _options(args)
    except (ValueError, OSError) as exc:
        if getattr(args, 'json', False):
            print(json.dumps({'error': str(exc)}))
        else:
            print(f'Error: {exc}')
        return 1
    preview = _preview(db, filters, ids, sweep)
    if args.dry_run:
        if getattr(args, 'json', False):
            print(json.dumps(preview))
        else:
            _print_preview(preview, filters, dry_run=True)
        return
    if not args.yes:
        _print_preview(preview, filters, dry_run=False)
        refused = _confirm_or_exit_code(
            f"Delete {len(preview['candidates'])} session(s) and close {preview['closed']} stale open session(s)? [y/N] ")
        if refused is not None:
            return refused
    params = {'filters': filters, 'session_ids': ids, 'sweep_idle_seconds': sweep}
    owner = getattr(args, 'owner_endpoint', None)
    try:
        if owner is not None:
            result = owner_prune(owner, timeout=timeout, **params)
        else:
            result = asyncio.run(run_prune_batches(db, filters, sessions_dir=_sessions_dir(),
                                                  session_ids=ids, sweep_idle_seconds=sweep))
    except (GatewayClientError, OSError, TimeoutError) as exc:
        if getattr(args, 'json', False):
            print(json.dumps({'error': str(exc), 'interrupted': True}))
            return 1
        return report_owner_failure(exc)
    if getattr(args, 'json', False):
        print(json.dumps(result))
    else:
        print(f"Pruned {result['deleted']} session(s).")
        if result['skipped']:
            print(f"Skipped {result['skipped']} session(s): {', '.join(result['skipped_ids'])}")
        if result['closed']:
            print(f"Closed {result['closed']} stale open session(s); transcripts kept for recovery.")
        if result['interrupted']:
            print(f"Gateway draining; stopped after the current batch ({result['remaining']} session(s) remain).")
    return 1 if result['interrupted'] else None
