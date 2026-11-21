"""Durable restart-cancellation receipts for the legacy, deliberately process-local queue.

These are NOT a second execution queue. The existing state_meta store holds the acceptance
obligation before we acknowledge it. A missing runtime cancels an undrained input; it never
replays it. Dispatch transfers to the ordinary turn/crash-marker path. No authority epoch is
created here: doing so would retire the canonical gateway sharing this database.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager

from persistence.sqlite._backend import SQLiteStore
from persistence.sqlite._state.common import LEGACY_QUEUE_RECEIPT_PREFIX as _PREFIX
from persistence.sqlite.conversation._store.compression import get_compression_lineage
from tui_gateway.store_handles import borrow_persistence


@contextmanager
def _receipt_holder(db):
    with borrow_persistence(db) as persistence:
        store = persistence._bound_store() if persistence is not None else None
        if not isinstance(store, SQLiteStore):
            raise RuntimeError("local queue receipts require SQLite session storage")
        yield store.holder()


def _key(receipt):
    return _PREFIX + receipt['session_id'] + ':' + receipt['input_id']


def accept(db, session, envelope):
    """Commit before acknowledgement, including isolated-compute queues with no user row."""
    existing = envelope.get('_queue_receipt')
    from tui_gateway.turn_marker import _writer_identity
    receipt = {**(existing or {}), 'input_id': existing['input_id'] if existing else uuid.uuid4().hex,
               'session_id': existing['session_id'] if existing else session['session_key'],
               'runtime_id': session.setdefault('_queue_owner', uuid.uuid4().hex),
               'status': 'queued', 'outcome': None, 'user': envelope['text'], **_writer_identity()}
    with _receipt_holder(db) as holder:
        holder._execute_write(lambda conn: conn.execute(
            'INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (_key(receipt), json.dumps(receipt))))
    envelope['_queue_receipt'] = receipt
    return receipt


def disposition(db, envelope, outcome):
    """Write the queue's cancellation or transfer BEFORE dropping its envelope."""
    receipt = envelope.get('_queue_receipt')
    if receipt is None:
        return
    status = {'dispatched': 'transferred', 'unknown': 'unknown'}.get(outcome, 'terminal')
    updated = {**receipt, 'status': status, 'outcome': None if outcome == 'unknown' else outcome}
    def write(conn):
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (_key(receipt),)).fetchone()
        if row is not None and (stored := json.loads(row['value']))['status'] == 'terminal':
            return stored  # A lost dispatch acknowledgement cannot undo an already committed completion.
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(updated), _key(receipt)))
        return updated
    with _receipt_holder(db) as holder:
        envelope['_queue_receipt'] = holder._execute_write(write)


def snapshot(db, session_id, live_sessions):
    """Recover undrained obligations, even after transcript cleanup deactivated their user rows.

    A same-process detached runtime is still an owner; a new window with the same stored
    session id cannot inherit an old promise. A foreign writer must be provably dead.
    """
    def read(conn):
        from tui_gateway.turn_marker import marker_writer_state
        out = []
        placeholders = ','.join('?' for _ in lineage)
        rows = conn.execute(f'SELECT key,value FROM state_meta WHERE key LIKE ? '
                            f'AND json_extract(value,\'$.session_id\') IN ({placeholders})',
                            (_PREFIX + '%', *lineage)).fetchall()
        for stored in rows:
            receipt = json.loads(stored['value'])
            runtime = next((r for r in list(live_sessions.values())
                            if r.get('_queue_owner') == receipt['runtime_id'] and not r.get('_finalized')), None)
            lost = runtime is None and (receipt.get('writer_pid') == os.getpid()
                                       or marker_writer_state(receipt) == 'dead')

            unresolved = runtime is not None and receipt['input_id'] in runtime.get('_unsettled_local_queue', {})
            if unresolved and receipt['status'] not in {'terminal', 'unknown'}:
                receipt.update(status='unknown', outcome=None, reason='terminal_receipt_unsaved')
            elif receipt['status'] == 'queued' and lost:
                receipt.update(status='terminal', outcome='cancelled', reason='backend_restart')
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), stored['key']))
            elif receipt['status'] == 'transferred' and lost:
                # The queue handed off to a local turn. Without its terminal receipt we cannot
                # assert it completed, or cancel/replay possible side effects.
                receipt.update(status='unknown', outcome=None, reason='backend_restart_after_dispatch')
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), stored['key']))
            out.append({k: v for k, v in receipt.items()
                        if k not in ('runtime_id', 'writer_pid', 'writer_start_time')})
        return out
    with _receipt_holder(db) as holder:
        lineage = get_compression_lineage(holder, session_id) or [session_id]
        return holder._execute_write(read)
