"""Only explicit completion/stop/restart receipts assign terminal launch causes."""
import time

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch

DDL = '''CREATE TABLE IF NOT EXISTS work_launches (
    launch_id TEXT PRIMARY KEY, kind TEXT NOT NULL, launcher_profile TEXT NOT NULL,
    launcher_session_id TEXT NOT NULL, parent_launch_id TEXT, child_profile TEXT NOT NULL,
    child_session_id TEXT, backend_ref TEXT, batch_id TEXT, batch_name TEXT, title TEXT,
    created_at REAL NOT NULL, bound_at REAL, finished_at REAL, terminal_cause TEXT, next_run_at REAL
)'''


def fixture_launch(db, sid, *, backend_ref=None, lid='launch'):
    def write(conn):
        conn.execute(DDL)
        conn.execute('INSERT INTO work_launches(launch_id,kind,launcher_profile,launcher_session_id,child_profile,'
                     'child_session_id,backend_ref,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (lid, 'lane', 'default', 'parent', 'default', sid, backend_ref, time.time()))
    db._execute_write(write)


def cause(db, lid='launch'):
    return db._read_one('SELECT terminal_cause FROM work_launches WHERE launch_id=?', (lid,))[0]


def test_orphan_reap_is_restart_only_for_unfinished_work_and_cleanup_is_not_success(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        for sid in ('unfinished', 'complete', 'cleanup', 'compressed'):
            db.create_session(sid, 'cli')
            fixture_launch(db, sid, lid=sid)
        db._execute_write(lambda conn: conn.execute(
            "UPDATE work_launches SET finished_at=?,terminal_cause='success' WHERE launch_id='complete'", (time.time(),)))
        db.end_session('unfinished', 'startup_orphan_reap')
        assert cause(db, 'unfinished') == 'owner_restart'
        db.end_session('complete', 'ws_orphan_reap')
        assert cause(db, 'complete') == 'success'
        db.end_session('cleanup', 'agent_close')
        assert cause(db, 'cleanup') == 'unknown'
        db.end_session('compressed', 'compression')
        assert cause(db, 'compressed') is None


@pytest.mark.parametrize(('source', 'expected'), [('process.kill', 'user_stop'), ('kill_all', 'parent_stop')])
def test_process_stop_source_is_not_provider_error(tmp_path, monkeypatch, source, expected):
    from gateway.session_context import scoped_current_session_id
    from tools.process_registry import ProcessRegistry
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    with scoped_current_session_id('parent'):
        process = registry._new_session('fixture command', 'task', 'task', 'route', str(tmp_path))
    with SessionDB(tmp_path / 'state.db') as db:
        if not getattr(process, 'launch_id', None):
            process.launch_id = 'process:' + process.id
            fixture_launch(db, None, backend_ref=process.id, lid=process.launch_id)
        registry._running[process.id] = process
        registry._finish_exited(process, -15, reason='killed', source=source)
        assert cause(db, process.launch_id) == expected


def test_managed_child_reaped_by_new_owner_keeps_completed_sibling_receipt(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = begin_runtime_epoch(db, instance_id='old')
        for i, sid in enumerate(('running-child', 'finished-child')):
            db.create_session(sid, 'subagent')
            fixture_launch(db, sid, lid=sid)
            db._execute_write(lambda conn, sid=sid, i=i: conn.execute(
                'INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) '
                'VALUES(?,?,?,?,0,?,?)', ('worker/child:' + str(i), sid, 'child', epoch, 'running', 'fixture')))
        db._execute_write(lambda conn: conn.execute(
            "UPDATE work_launches SET finished_at=?,terminal_cause='success' WHERE launch_id='finished-child'", (time.time(),)))
        begin_runtime_epoch(db, instance_id='new')
        assert cause(db, 'running-child') == 'owner_restart'
        assert cause(db, 'finished-child') == 'success'
