"""Family provenance is additive, idempotent and fenced like other state writes."""
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError, mutate_worker_execution
from tests.hermes_state.test_runtime_worker_lifecycle import setup_store


def launches(db):
    with db._read_ctx() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM work_launches ORDER BY launch_id')]


def test_existing_session_entrypoint_exposes_nullable_step_columns(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('child', 'cli')
        row = db.get_session('child')
        assert 'last_activity_kind' in row
        assert row['last_activity_kind'] is None
        assert row['last_activity_tool'] is None
        with db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM work_launches').fetchone()[0] == 0


def test_launch_survives_early_record_retries_binding_and_first_terminal_receipt(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        fields = dict(kind='teammate', launcher_profile='default', launcher_session_id='outside',
                      child_profile='fixture', title='x' * 60)
        assert db.record_launch('launch', **fields) == 'launch'
        assert launches(db)[0]['child_session_id'] is None
        db.record_launch('launch', **fields)
        assert len(launches(db)) == 1
        assert launches(db)[0]['title'] == 'x' * 39 + '…'
        db.create_session('child', 'cli')
        db.bind_launch('launch', child_session_id='child', backend_ref='exact')
        db.bind_launch('launch', child_session_id='child', backend_ref='exact')
        with pytest.raises(ValueError):
            db.bind_launch('launch', child_session_id='different')
        db.finish_launch('launch', 'success')
        first = launches(db)[0]
        db.finish_launch('launch', 'owner_restart')
        assert launches(db)[0] == first
        assert first['terminal_cause'] == 'success'
        with pytest.raises(ValueError):
            db.record_launch('launch', **dict(fields, launcher_session_id='other'))
        db.delete_session('child')
        assert db.get_session('child') is None
        assert launches(db)[0]['terminal_cause'] == 'success'


def test_reconcile_legacy_session_table_before_indexes(tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(path):
        pass
    conn = sqlite3.connect(path)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(sessions)')}
    for column in ('last_activity_kind', 'last_activity_tool'):
        if column in columns:
            conn.execute(f'ALTER TABLE sessions DROP COLUMN {column}')
    conn.execute('DROP TABLE IF EXISTS work_launches')
    conn.commit()
    conn.close()
    with SessionDB(path) as db:
        db.create_session('migrated', 'cli')
        assert db.get_session('migrated')['last_activity_kind'] is None
        assert launches(db) == []


def test_managed_launches_use_closed_receipts_and_cannot_mutate_foreign_launch(tmp_path):
    db, store, scope = setup_store(tmp_path)
    try:
        fields = dict(kind='process', launcher_profile='fixture', launcher_session_id='owned',
                      child_profile='fixture', title='local command')
        store.record_launch('mine', **fields)
        store.bind_launch('mine', backend_ref='process-exact')
        store.finish_launch('mine', 'error')
        assert launches(db)[0]['terminal_cause'] == 'error'
        db.record_launch('foreign', **dict(fields, launcher_session_id='foreign'))
        for operation, payload in [('launch.finish', {'launch_id': 'foreign', 'terminal_cause': 'success'}),
                                   ('launch.bind', {'launch_id': 'foreign', 'child_session_id': 'owned', 'backend_ref': None}),
                                   ('launch.record', dict(fields, launch_id='bad', arbitrary=True))]:
            with pytest.raises(RuntimeStoreError, match='permission_denied|invalid_params'):
                mutate_worker_execution(db, **scope, sequence=store.journal['next_sequence'],
                                        operation=operation, payload=payload)
        assert next(r for r in launches(db) if r['launch_id'] == 'foreign')['finished_at'] is None
    finally:
        store.close()
        db.close()


def test_managed_default_profile_and_absent_finish_are_idempotent(tmp_path):
    db, store, scope = setup_store(tmp_path)
    try:
        db._write_sql('UPDATE sessions SET profile_name=NULL WHERE id=?', ('owned',))
        store.record_launch('default-process', kind='process', launcher_profile='default',
                            launcher_session_id='owned', child_profile='default')
        store.finish_launch('absent-metadata', 'unknown')
        assert len(launches(db)) == 1
        assert launches(db)[0]['child_profile'] == 'default'
    finally:
        store.close()
        db.close()
