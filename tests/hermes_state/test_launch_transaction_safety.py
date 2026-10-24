"""Optional launch failures preserve the owning transaction, even when SQLite aborts it."""
import json
import sqlite3
from dataclasses import asdict
from functools import partial

import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt
from hermes_state_local import commit_local_session


def local_receipt(tmp_path, *, cron=False):
    from gateway.config import Platform
    from gateway.session import SessionEntry, SessionSource
    from gateway.session_lifecycle import _now
    from gateway.session_policy import build_policy
    request = dict(source='cli', cwd=str(tmp_path), model='fixture', toolsets=[],
                   started_by_session='parent', started_by_profile='default', launch_kind='lane', launch_id='launch')
    source = SessionSource(platform=Platform.LOCAL, chat_id='child', user_id='human', chat_type='dm')
    entry = SessionEntry('local:child', 'child', _now(), _now(), origin=source, platform=Platform.LOCAL)
    policy = asdict(build_policy(request, {'platform_toolsets': {'cli': []}}, private_secrets={}))
    if cron:
        policy['request_json'] = json.dumps(dict(request, request_id='run', cron_job=dict(
            id='job', creator_profile='default', creator_session_id='parent', launch_title='fixture'),
            started_by_session=None))
    return dict(profile_id='profile', principal_id='human', request_id='create', session_id='child',
                route=entry.session_key, entry=entry.to_dict(), policy=policy)


def snapshot(db):
    tables = ('sessions', 'gateway_routing', 'state_meta', 'session_admissions',
              'worker_executions', 'worker_receipts', 'work_launches')
    with db._read_ctx() as conn:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM {table}')), key=repr)
                for table in tables if table in existing}


def launch(db, sid='child', kind='helper'):
    db.record_launch('launch', kind=kind, launcher_profile='default', launcher_session_id='parent',
                     child_profile='default')
    db.bind_launch('launch', child_session_id=sid)


def operation(db, tmp_path, endpoint):
    binding = endpoint.endswith('_bind')
    endpoint = endpoint.removesuffix('_bind')
    epoch = rt.begin_runtime_epoch(db, instance_id='fixture')
    if endpoint in {'create', 'cron'}:
        receipt = local_receipt(tmp_path, cron=endpoint == 'cron')
        return partial(commit_local_session, db, epoch=epoch, receipt=receipt), 'UPDATE' if binding else 'INSERT'
    db.create_session('child', 'cli')
    launch(db)
    if endpoint == 'end':
        return partial(db.end_session, 'child', 'user_stop'), 'UPDATE'
    if endpoint == 'settle':
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='child',
                               request_id='turn', payload={'text': 'fixture'})
        claimed = rt.claim_session_input(db, epoch=epoch, session_id='child')
        return partial(rt.settle_session_input, db, epoch=epoch, admission_id=claimed['admission_id'],
                       generation=claimed['generation'], outcome='completed', result={'completed': True}), 'UPDATE'
    from gateway.session_worker_child import reserve_child_execution
    from tests.hermes_state.test_runtime_worker_children import _reserve
    db.create_session('parent', 'cli')
    rt.register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='parent', generation=0,
                                 kind='compute', adoption_secret='private-fixture')
    if endpoint == 'reserve':
        return partial(_reserve, db, epoch, 1), 'UPDATE' if binding else 'INSERT'
    handler = partial(reserve_child_execution, epoch=epoch, execution_id='worker', identity={},
                      principal='human', limits=(2, 1), refused=None)
    child = db._execute_write(lambda conn: handler(db, conn, 'parent', {'secret': 'fixture-child-secret'}))['child']
    db.bind_launch('launch', child_session_id='child')
    db.record_launch('reserved', kind='helper', launcher_profile='default', launcher_session_id='parent',
                     child_profile='default')
    db.bind_launch('reserved', child_session_id=child['session_id'])
    return partial(rt.mutate_worker_execution, db, epoch=epoch, execution_id='worker', session_id='parent',
                   generation=0, sequence=1, operation='execution.finish', payload={}), 'UPDATE'


class FullLaunchConnection:
    def __init__(self, conn, statement):
        self.conn = conn
        self.statement = statement
        self.aborted = False

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, parameters=()):
        prefix = 'INSERT INTO work_launches' if self.statement == 'INSERT' else 'UPDATE work_launches'
        if sql.startswith(prefix):
            parameters = list(parameters)
            parameters[0] = bytes(1048576)
            try:
                return self.conn.execute(sql, parameters)
            except sqlite3.Error:
                self.aborted = not self.conn.in_transaction
                raise
        return self.conn.execute(sql, parameters)


def install_fault(db, statement, fault):
    db._write_sql('CREATE TABLE launch_fault(payload BLOB)')
    if fault == 'full':
        pages = db._conn.execute('PRAGMA page_count').fetchone()[0]
        db._conn.execute(f'PRAGMA max_page_count={pages + 8}')
        db._conn = FullLaunchConnection(db._conn, statement)
        return db._conn
    body = "INSERT INTO launch_fault VALUES('partial'); SELECT RAISE(FAIL,'launch fixture failure');"
    db._write_sql(f'CREATE TRIGGER fail_launch BEFORE {statement} ON work_launches BEGIN {body} END')


@pytest.mark.parametrize('endpoint', ['create', 'create_bind', 'cron', 'cron_bind', 'end', 'settle', 'reserve', 'reserve_bind', 'retire'])
def test_disk_full_launch_never_continues_the_real_work_in_autocommit(tmp_path, endpoint):
    with SessionDB(tmp_path / 'state.db') as db:
        action, statement = operation(db, tmp_path, endpoint)
        fault = install_fault(db, statement, 'full')
        before = snapshot(db)
        error = None
        try:
            action()
        except sqlite3.Error as exc:
            error = exc
        after = snapshot(db)
        assert fault.aborted, 'the injected launch statement did not abort the SQLite transaction'
        assert after == before, f'{endpoint} split the caller transaction after SQLITE_FULL'
        assert error is not None and error.sqlite_errorcode == sqlite3.SQLITE_FULL
        assert not db._conn.in_transaction


@pytest.mark.parametrize('endpoint', ['create', 'create_bind', 'cron', 'cron_bind', 'end', 'settle', 'reserve', 'reserve_bind', 'retire'])
def test_recoverable_launch_statement_rolls_back_only_optional_effects(tmp_path, endpoint):
    with SessionDB(tmp_path / 'state.db') as db:
        action, statement = operation(db, tmp_path, endpoint)
        install_fault(db, statement, 'fail')
        before = snapshot(db)
        action()
        assert snapshot(db) != before, 'the real work did not commit'
        assert db._read_one('SELECT COUNT(*) FROM launch_fault')[0] == 0, 'partial metadata escaped its savepoint'
        if endpoint.removesuffix('_bind') in {'create', 'cron'}:
            assert db.get_session('child') is not None
            saved = db._read_one('SELECT value FROM state_meta WHERE key=?', ('gateway.local_policy.v1:child',))
            assert json.loads(saved[0])['session_id'] == 'child'
            assert db._read_one('SELECT COUNT(*) FROM work_launches')[0] == 0


@pytest.mark.parametrize('code', [sqlite3.SQLITE_IOERR, sqlite3.SQLITE_NOMEM, sqlite3.SQLITE_INTERRUPT])
def test_injected_transaction_abort_propagates_the_original_error_once(tmp_path, monkeypatch, caplog, code):
    import hermes_state_launches
    import hermes_state_optional_launch
    error = sqlite3.OperationalError('injected transaction-aborting launch failure')
    error.sqlite_errorcode = code
    def aborted(conn, **payload):
        conn.rollback()
        raise error
    monkeypatch.setattr(hermes_state_launches, 'record_launch', aborted)
    monkeypatch.setattr(hermes_state_optional_launch, '_warnings', {})
    with SessionDB(tmp_path / 'state.db') as db:
        action, _statement = operation(db, tmp_path, 'create')
        before = snapshot(db)
        caught = None
        try:
            action()
        except sqlite3.Error as exc:
            caught = exc
        assert snapshot(db) == before, 'an injected abort split the real creation receipt'
        assert caught is error
        warnings = [r for r in caplog.records if r.name == 'hermes_state_optional_launch']
        assert len(warnings) == 1


def test_real_work_failure_rolls_back_the_same_receipt_without_launch_metadata(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='fixture')
        db.create_session('child', 'cli')
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='child',
                               request_id='turn', payload={'text': 'fixture'})
        claimed = rt.claim_session_input(db, epoch=epoch, session_id='child')
        db._write_sql('CREATE TABLE real_work(payload BLOB)')
        pages = db._conn.execute('PRAGMA page_count').fetchone()[0]
        db._conn.execute(f'PRAGMA max_page_count={pages + 8}')
        before = snapshot(db)
        def failed_real_work(conn, row):
            conn.execute('INSERT INTO real_work VALUES(zeroblob(1048576))')
        with pytest.raises(sqlite3.OperationalError) as caught:
            rt.settle_session_input(db, epoch=epoch, admission_id=claimed['admission_id'],
                                    generation=claimed['generation'], outcome='completed',
                                    _terminal_write=failed_real_work)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_FULL
        assert snapshot(db) == before
