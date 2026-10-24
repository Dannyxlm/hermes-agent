"""Selector-only rollback may delete settled history, never unresolved runtime work."""
import sqlite3
from contextlib import closing

import pytest

import hermes_state_runtime as rt
from hermes_state import SessionDB
from hermes_state_errors import classify_persistence_error


def _settled(db, epoch, sid, source='rollback-probe'):
    db.create_session(sid, source=source)
    db.append_message(sid, role='user', content='synthetic')
    accepted = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id=sid,
                                      request_id='r', payload={'text': 'synthetic'})
    claim = rt.claim_session_input(db, epoch=epoch, session_id=sid)
    rt.settle_session_input(db, epoch=epoch, admission_id=accepted['admission_id'],
                            generation=claim['generation'], outcome='completed')
    scope = dict(epoch=epoch, execution_id='worker:' + sid, session_id=sid,
                 generation=claim['generation'])
    rt.register_worker_execution(db, **scope, kind='compute', adoption_secret='synthetic-fixture')
    rt.mutate_worker_execution(db, **scope, sequence=1, operation='execution.finish', payload={})
    assert _ledger_counts(db, sid) == (1, 1, 1)
    return accepted['admission_id']


def _ledger_counts(db, sid):
    return tuple(db._read_one('SELECT '
        '(SELECT COUNT(*) FROM session_admissions WHERE target_session_id=?), '
        '(SELECT COUNT(*) FROM worker_executions WHERE session_id=?), '
        '(SELECT COUNT(*) FROM worker_receipts WHERE execution_id IN '
        '(SELECT execution_id FROM worker_executions WHERE session_id=?))', (sid, sid, sid)))


def _older_release_delete(path, sid):
    """Raw legacy transaction also proves the guard works without candidate retirement code."""
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?', (sid,))
            conn.execute('DELETE FROM messages WHERE session_id = ?', (sid,))
            conn.execute('DELETE FROM sessions WHERE id = ?', (sid,))
            conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK')
            raise


def test_older_delete_retires_terminal_history_only_for_its_target(tmp_path):
    path = tmp_path / 'state.db'
    with closing(SessionDB(path)) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        for sid in ('settled', 'untouched'):
            _settled(db, epoch, sid)
        db.create_session('plain', source='cli')
    _older_release_delete(path, 'settled')
    _older_release_delete(path, 'plain')
    with closing(SessionDB(path)) as db:
        assert db.get_session('settled') is None and db.get_messages('settled') == []
        assert db.get_session('plain') is None
        assert _ledger_counts(db, 'settled') == (0, 0, 0)
        assert _ledger_counts(db, 'untouched') == (1, 1, 1)
        assert db._read_one('PRAGMA quick_check')[0] == 'ok'
        assert not db._read_one('PRAGMA foreign_key_check')


@pytest.mark.parametrize('table,status', [
    ('session_admissions', 'queued'), ('session_admissions', 'started'),
    ('session_admissions', 'unknown'), ('worker_executions', 'registered'),
    ('worker_executions', 'running'), ('worker_executions', 'unknown'),
])
def test_older_delete_refuses_every_nonterminal_status_atomically(tmp_path, table, status):
    path = tmp_path / 'state.db'
    with closing(SessionDB(path)) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        _settled(db, epoch, 'busy')
        column = 'target_session_id' if table == 'session_admissions' else 'session_id'
        # Exercise every persisted state independently, including terminal rows alongside it.
        db._execute_write(lambda c: c.execute(
            f'UPDATE {table} SET status=? WHERE {column}=?', (status, 'busy')))
    with pytest.raises(sqlite3.IntegrityError, match='unresolved gateway runtime work') as refused:
        _older_release_delete(path, 'busy')
    assert classify_persistence_error(refused.value) == 'unknown'
    with closing(SessionDB(path)) as db:
        assert len(db.get_messages('busy')) == 1
        assert _ledger_counts(db, 'busy') == (1, 1, 1)
        assert db._read_one(f'SELECT status FROM {table} WHERE {column}=?', ('busy',))[0] == status


def test_an_upgraded_store_replaces_the_old_guard_on_open(tmp_path):
    path = tmp_path / 'state.db'
    with closing(SessionDB(path)) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        _settled(db, epoch, 's')
        db._execute_write(lambda c: c.execute('DROP TRIGGER sessions_runtime_ledger_delete_guard'))
        db._execute_write(lambda c: c.execute('''
            CREATE TRIGGER sessions_runtime_ledger_delete_guard BEFORE DELETE ON sessions
            BEGIN SELECT RAISE(ABORT, 'old unconditional refusal'); END;
        '''))
    SessionDB(path).close()
    _older_release_delete(path, 's')


@pytest.mark.parametrize('operation', ['delete', 'bulk', 'prune'])
def test_candidate_still_publishes_retirement_before_parent_delete(tmp_path, operation):
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        admission_id = _settled(db, epoch, 's')
        db.end_session('s', 'done')
        # An independent assertion at the parent boundary: candidate retirement already removed
        # all ledger rows, so the rollback trigger has nothing to retire on these paths.
        db._execute_write(lambda c: c.execute('''
            CREATE TRIGGER assert_candidate_retired BEFORE DELETE ON sessions
            WHEN EXISTS (SELECT 1 FROM session_admissions WHERE target_session_id=old.id)
              OR EXISTS (SELECT 1 FROM worker_executions WHERE session_id=old.id)
            BEGIN SELECT RAISE(ABORT, 'candidate skipped retirement'); END;
        '''))
        if operation == 'delete':
            assert db.delete_session('s')
        elif operation == 'bulk':
            assert db.delete_sessions(['s']) == 1
        else:
            assert db.prune_sessions(older_than_days=None) == 1
        assert db.get_session('s') is None
        assert rt.get_session_admission(db, admission_id=admission_id)['status'] == 'terminal'
