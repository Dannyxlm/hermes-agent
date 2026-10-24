"""Shared cron jobs keep creator provenance while attempts retain their immediate trigger."""
from contextlib import contextmanager
from datetime import datetime

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError


def writer(db):
    @contextmanager
    def scope():
        yield db
    return scope


def test_worker_can_refresh_shared_job_but_not_a_foreign_profile(tmp_path, monkeypatch):
    from tests.hermes_state.test_runtime_worker_lifecycle import setup_store
    from cron import jobs
    import hermes_state_launch_context
    db, store, scope = setup_store(tmp_path)
    (tmp_path / 'profiles' / 'fixture').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profiles' / 'fixture'))
    monkeypatch.setattr(hermes_state_launch_context, 'launch_writer', writer(db))
    try:
        with scoped_current_session_id('previous-creator'):
            first = jobs.create_job('First fixture', 'every 2h')
        monkeypatch.setattr(hermes_state_launch_context, 'launch_writer', writer(store))
        with scoped_current_session_id('owned'):
            second = jobs.create_job('Second fixture', 'every 3h')
            paused = jobs.pause_job(first['id'])
        row = db._read_one('SELECT * FROM work_launches WHERE backend_ref=?', (first['id'],))
        assert row['launcher_session_id'] == 'previous-creator'
        assert row['next_run_at'] == datetime.fromisoformat(paused['next_run_at']).timestamp()
        jobs.remove_job(first['id'])
        assert db._read_one('SELECT terminal_cause FROM work_launches WHERE backend_ref=?', (first['id'],))[0] == 'user_stop'
        assert db._read_one('SELECT next_run_at FROM work_launches WHERE backend_ref=?', (second['id'],))[0] is not None
        db.record_launch('foreign-profile-job', kind='check_in', launcher_profile='foreign',
                         launcher_session_id='foreign', child_profile='foreign', backend_ref='foreign-job')
        from hermes_state_runtime import mutate_worker_execution
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            mutate_worker_execution(db, **scope, sequence=store.journal['next_sequence'],
                operation='launch.schedule', payload={'launch_id': 'foreign-profile-job', 'next_run_at': None})
    finally:
        store.close()
        db.close()


@pytest.mark.parametrize('source', ['manual', 'builtin'])
def test_manual_run_keeps_trigger_and_finishes_after_job_removal(tmp_path, monkeypatch, source):
    from cron import jobs, executions
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with scoped_current_session_id('job-creator'):
        job = jobs.create_job('Manual fixture', 'every 2h')
    if source == 'builtin':
        with scoped_current_session_id('manual-trigger'):
            jobs.trigger_job(job['id'])
        run = executions.create_execution(job['id'], source=source)
    else:
        with scoped_current_session_id('manual-trigger'):
            run = executions.create_execution(job['id'], source=source)
    executions.mark_execution_running(run['id'])
    jobs.remove_job(job['id'])
    executions.finish_execution(run['id'], success=True)
    with SessionDB(tmp_path / 'state.db') as db:
        row = db._read_one('SELECT * FROM work_launches WHERE backend_ref=?', (run['id'],))
        assert row['kind'] == 'check_in_run'
        assert row['launcher_session_id'] == 'manual-trigger'
        assert row['parent_launch_id'] == 'cron-job:' + job['id']
        assert row['terminal_cause'] == 'success'
        assert db._read_one('SELECT COUNT(*) FROM work_launches')[0] == 2


def test_job_creator_cannot_change_during_update(tmp_path, monkeypatch):
    from cron import jobs
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with scoped_current_session_id('creator'):
        job = jobs.create_job('Immutable fixture', 'every 2h')
    with pytest.raises(ValueError):
        jobs.update_job(job['id'], {'creator_session_id': 'replacement'})
    assert jobs.get_job(job['id'])['creator_session_id'] == 'creator'


def test_unlinked_legacy_execution_does_not_create_a_session_store(tmp_path, monkeypatch):
    from cron import executions
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    run = executions.create_execution('legacy-unlinked-job', source='builtin')
    executions.finish_execution(run['id'], success=True)
    assert not (tmp_path / 'state.db').exists()


def test_unstarted_cron_attempt_has_failed_start_receipt(tmp_path, monkeypatch):
    from cron import jobs, executions
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with scoped_current_session_id('job-creator'):
        job = jobs.create_job('Unstarted fixture', 'every 2h')
    run = executions.create_execution(job['id'], source='scheduled')
    executions.settle_unstarted_execution(run['id'], job['id'], 'fixture dispatch refusal')
    with SessionDB(tmp_path / 'state.db') as db:
        row = db._read_one('SELECT terminal_cause FROM work_launches WHERE backend_ref=?', (run['id'],))
        assert row[0] == 'failed_start'
