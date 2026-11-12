"""Interrupted pruning keeps fences, exact scope, and cold metadata reads off the writer."""
import json
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_local import API_DECLARED_PREFIX
from hermes_state_common import LEGACY_QUEUE_RECEIPT_PREFIX
from hermes_state_mutation_retirement import RETIRED_PREFIX, prune_retired_tombstones
from hermes_state_prune import PENDING_PREFIX
from hermes_state_runtime import RuntimeStoreError
from tests.hermes_state.test_prune_bounded_batches import ended, local_group


@pytest.mark.parametrize('delete', ['unbounded', 'explicit', 'discard', 'moved'])
def test_other_delete_paths_remove_pending_before_id_reuse(tmp_path, delete):
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 's', messages=5, source='api_server')
        assert db.prune_sessions(older_than_days=None, message_cap=2)['in_progress_ids'] == ['s']
        if delete == 'unbounded':
            assert db.prune_sessions(older_than_days=None) == 1
        elif delete == 'explicit':
            assert db.delete_session('s')
        elif delete == 'discard':
            assert db.discard_unadmitted_session('s')
        else:
            assert db.delete_moved_session('s')
        assert db.get_meta(PENDING_PREFIX + 's') is None
        db._execute_write(lambda conn: prune_retired_tombstones(conn, older_than=0))
        # Explicit deletion fences are permanent; this fixture simulates a historic stale key too.
        db._write_sql('DELETE FROM state_meta WHERE key=?', (RETIRED_PREFIX + 's',))
        ended(db, 's', source='api_server')
        db.set_meta(PENDING_PREFIX + 's', json.dumps(['s']))
        db.save_gateway_routing_entry('route:s', '{"session_id":"s"}')
        result = db.prune_sessions(older_than_days=None, message_cap=2, session_ids=['s'])
        assert result['deleted_ids'] == ['s']
        assert db.get_meta(RETIRED_PREFIX + 's') is not None
        assert 'route:s' not in db.load_gateway_routing_entries()


def test_stale_pending_key_does_not_replace_actual_group_membership(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        local_group(db, ['s', 'outside'])
        db.set_meta(PENDING_PREFIX + 's', json.dumps(['s']))
        result = db.prune_sessions(older_than_days=None, message_cap=2, session_ids=['s'])
        assert result['deleted_ids'] == []
        assert result['skipped_ids'] == ['s']
        assert db.get_session('s') and db.get_session('outside')


@pytest.mark.parametrize('kind', ['local', 'compression'])
@pytest.mark.parametrize('missing', ['a', 'b'])
def test_pending_group_recovers_after_one_member_is_explicitly_deleted(tmp_path, kind, missing):
    with SessionDB(tmp_path / 'state.db') as db:
        if kind == 'local':
            local_group(db, ['a', 'b'])
            for n in range(4):
                db.append_message('b', 'tool', str(n), timestamp=1)
        else:
            ended(db, 'a', messages=3)
            ended(db, 'b', messages=3, parent_session_id='a')
            db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='a'")
        assert db.prune_sessions(older_than_days=None, message_cap=2)['in_progress_ids'] == ['a', 'b']
        assert db.delete_session(missing)
        survivor = 'b' if missing == 'a' else 'a'
        results = []
        for _ in range(5):
            result = db.prune_sessions(older_than_days=None, message_cap=2, session_ids=[survivor])
            results.extend(result['deleted_ids'])
            if not result['has_more']:
                break
        assert results == [survivor]
        assert db.get_meta(PENDING_PREFIX + 'a') is None


@pytest.mark.parametrize('operation', ['reopen', 'recover', 'pin', 'archive', 'auto_archive'])
def test_fenced_retained_session_cannot_change_eligibility(tmp_path, operation):
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 's', messages=5)
        db._write_sql("UPDATE sessions SET end_reason=?,archived=? WHERE id='s'",
                      ('agent_close' if operation == 'recover' else 'cli_close', int(operation == 'recover')))
        assert db.prune_sessions(older_than_days=None, message_cap=2)['in_progress_ids'] == ['s']
        before = db.get_session('s')
        calls = {'reopen': lambda: db.reopen_if_explicitly_closed('s', provenance='test'),
                 'recover': lambda: db.unarchive_recoverable_session('s'),
                 'pin': lambda: db.set_session_pinned('s', True),
                 'archive': lambda: db.set_session_archived('s', True)}
        if operation == 'auto_archive':
            assert db.archive_stale_sessions(0) == 0
        else:
            with pytest.raises(RuntimeStoreError, match='not_found'):
                calls[operation]()
        after = db.get_session('s')
        assert {k: after[k] for k in ('ended_at', 'end_reason', 'archived', 'pinned')} == {
            k: before[k] for k in ('ended_at', 'end_reason', 'archived', 'pinned')}
        while db.prune_sessions(older_than_days=None, message_cap=2)['has_more']:
            pass
        assert db.get_session('s') is None


def test_orphan_sweep_skips_historic_fenced_open_row_and_closes_other_row(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        for sid in ['fenced', 'ordinary']:
            ended(db, sid)
            db._write_sql('UPDATE sessions SET ended_at=NULL WHERE id=?', (sid,))
        db.set_meta(RETIRED_PREFIX + 'fenced', json.dumps({'retired_at': 1}))
        assert db.sweep_orphaned_sessions(max_idle_seconds=1, sources=('cron',),
                                         respect_gateway_heartbeats=False) == ['ordinary']
        assert db.get_session('fenced')['ended_at'] is None


@pytest.mark.parametrize('scope', ['ids', 'source', 'pinned', 'archived'])
def test_pending_group_still_requires_whole_frozen_caller_scope(tmp_path, scope):
    with SessionDB(tmp_path / 'state.db') as db:
        local_group(db, ['a', 'b'])
        for _ in range(4):
            db.append_message('b', 'tool', 'x', timestamp=1)
        db.prune_sessions(older_than_days=None, message_cap=2)
        kwargs = {'session_ids': ['a', 'b']}
        if scope == 'ids':
            kwargs['session_ids'] = ['a']
        elif scope == 'source':
            db._write_sql("UPDATE sessions SET source='telegram' WHERE id='b'")
            kwargs['source'] = 'cron'
        elif scope == 'pinned':
            db._write_sql("UPDATE sessions SET pinned=1 WHERE id='b'")
        else:
            db._write_sql("UPDATE sessions SET archived=1 WHERE id='b'")
            kwargs['archived'] = False
        result = db.prune_sessions(older_than_days=None, message_cap=10, **kwargs)
        assert result['deleted_ids'] == []
        assert db.get_session('a') and db.get_session('b')


def test_batch_receipt_and_route_snapshots_are_not_scanned_or_parsed_under_writer(tmp_path, monkeypatch):
    with SessionDB(tmp_path / 'state.db') as db:
        for sid in ['a', 'b', 'c']:
            ended(db, sid)
            db.set_meta('gateway.mutation.v1.' + sid, json.dumps({'result': {'session_id': sid, 'title': 'private'}}))
            db.set_meta(API_DECLARED_PREFIX + sid, sid)
            db.set_meta(LEGACY_QUEUE_RECEIPT_PREFIX + sid, json.dumps({'session_id': sid}))
            db.save_gateway_routing_entry('route:' + sid, json.dumps({'session_id': sid}))
        for n in range(10):
            db.set_meta('gateway.mutation.v1.other' + str(n), json.dumps({'result': {'session_id': 'other', 'title': 'kept'}}))
        original, loads, statements = db._execute_write, json.loads, []
        broad_payloads = {row[0] for row in db._read_all(
            "SELECT value FROM state_meta WHERE key GLOB 'gateway.mutation.v1.*' OR key GLOB ?",
            (LEGACY_QUEUE_RECEIPT_PREFIX + '*',))}
        broad_payloads.update(row[0] for row in db._read_all('SELECT entry_json FROM gateway_routing'))
        def write(fn, *args, **kwargs):
            def checked(conn):
                conn.set_trace_callback(statements.append)
                try:
                    return fn(conn)
                finally:
                    conn.set_trace_callback(None)
            return original(checked, *args, **kwargs)
        monkeypatch.setattr(db, '_execute_write', write)
        # Broad JSON decoding belongs on the reader, not once per group under the write lock.
        def checked_loads(raw, *args, **kwargs):
            if isinstance(raw, str) and raw in broad_payloads:
                assert not db._conn.in_transaction, 'receipt/route JSON decoded under writer lock'
            return loads(raw, *args, **kwargs)
        monkeypatch.setattr(json, 'loads', checked_loads)
        assert db.prune_sessions(older_than_days=None, message_cap=10)['deleted_ids'] == ['a', 'b', 'c']
        assert not [sql for sql in statements if sql.startswith('SELECT') and any(
            prefix + '*' in sql for prefix in ('gateway.mutation.v1.', API_DECLARED_PREFIX, LEGACY_QUEUE_RECEIPT_PREFIX))]
        for sid in ['a', 'b', 'c']:
            assert json.loads(db.get_meta('gateway.mutation.v1.' + sid))['result']['title'] is None
            assert db.get_meta(API_DECLARED_PREFIX + sid) is None
            assert db.get_meta(LEGACY_QUEUE_RECEIPT_PREFIX + sid) is None
        assert db.load_gateway_routing_entries() == {}


def test_existing_store_installs_receipt_snapshot_invalidation(tmp_path):
    from hermes_state_prune_snapshot import receipts_version
    path = tmp_path / 'state.db'
    with SessionDB(path) as db:
        db._write_sql('DROP TRIGGER prune_receipts_insert')
        db._write_sql('DROP TRIGGER prune_receipts_update')
        db.set_meta('gateway.mutation.v1.receipt', json.dumps({'result': {'session_id': 'other'}}))
    for _ in range(2):
        with SessionDB(path) as db:
            before = db._read_one('SELECT value FROM state_meta WHERE key=?',
                                  ('maintenance.prune.receipts_version.v1',))
            version = before[0] if before else '0'
            db.set_meta('unrelated', 'value')
            db.save_gateway_routing_entry('route', '{"session_id":"other"}')
            with db._read_ctx() as conn:
                assert receipts_version(conn) == version
            db.set_meta('gateway.mutation.v1.receipt', json.dumps({'result': {'session_id': str(version)}}))
            with db._read_ctx() as conn:
                assert receipts_version(conn) != version
            db.set_meta(API_DECLARED_PREFIX + str(version), 's')
            with db._read_ctx() as conn:
                assert int(receipts_version(conn)) > int(version)


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('change', ['insert', 'repoint'])
def test_receipt_insert_between_snapshot_and_writer_is_not_missed(tmp_path, monkeypatch, external, change):
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 's')
        if change == 'repoint':
            db.set_meta('gateway.mutation.v1.late', json.dumps({'result': {'session_id': 'other', 'title': 'private'}}))
            db.set_meta(API_DECLARED_PREFIX + 'late', 'other')
            db.set_meta(LEGACY_QUEUE_RECEIPT_PREFIX + 'late', json.dumps({'session_id': 'other'}))
        original = db._execute_write
        inserted = False
        def write(fn, *args, **kwargs):
            nonlocal inserted
            if not inserted:
                inserted = True
                values = [('gateway.mutation.v1.late', json.dumps({'result': {'session_id': 's', 'title': 'private'}})),
                          (API_DECLARED_PREFIX + 'late', 's'),
                          (LEGACY_QUEUE_RECEIPT_PREFIX + 'late', json.dumps({'session_id': 's'}))]
                statement = ('UPDATE state_meta SET value=?2 WHERE key=?1' if change == 'repoint' else
                             'INSERT INTO state_meta(key,value) VALUES(?,?)')
                if external:
                    other = sqlite3.connect(db.db_path)
                    try:
                        with other:
                            other.executemany(statement, values)
                    finally:
                        other.close()
                else:
                    original(lambda conn: conn.executemany(statement, values))
            return original(fn, *args, **kwargs)
        monkeypatch.setattr(db, '_execute_write', write)
        for _ in range(3):
            result = db.prune_sessions(older_than_days=None, message_cap=2)
            if not result['has_more']:
                break
        assert db.get_session('s') is None
        assert json.loads(db.get_meta('gateway.mutation.v1.late'))['result']['title'] is None
        assert db.get_meta(API_DECLARED_PREFIX + 'late') is None
        assert db.get_meta(LEGACY_QUEUE_RECEIPT_PREFIX + 'late') is None
