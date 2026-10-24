"""Cron job provenance is separate from delivery; each exact fire links back to its job."""
from datetime import UTC, datetime, timedelta

from cron import jobs, executions
from hermes_state import SessionDB


def test_cli_and_tool_job_creator_is_retained_and_next_fire_tracks_saves(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_SESSION_ID', 'creator')
    job = jobs.create_job('fixture task', 'every 2h', name='short check-in')
    assert job.get('creator_session_id') == 'creator'
    assert job.get('creator_profile') == 'default'
    assert jobs.get_job(job['id'])['creator_session_id'] == 'creator'
    with SessionDB(tmp_path / 'state.db') as db:
        with db._read_ctx() as conn:
            row = dict(conn.execute('SELECT * FROM work_launches WHERE kind=?', ('check_in',)).fetchone())
        assert row['backend_ref'] == job['id']
        assert row['launcher_session_id'] == 'creator'
        assert row['next_run_at'] == datetime.fromisoformat(job['next_run_at']).timestamp()
        newer = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
        job['next_run_at'] = newer
        jobs.save_jobs([job])
        assert db._read_one('SELECT next_run_at FROM work_launches WHERE launch_id=?',
                            (row['launch_id'],))[0] == datetime.fromisoformat(newer).timestamp()


def test_script_only_execution_uses_exact_execution_id_without_session(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_SESSION_ID', 'creator')
    job = jobs.create_job('', 'every 2h', name='script fire', script='fixture.py', no_agent=True)
    fire = executions.create_execution(job['id'], source='manual')
    with SessionDB(tmp_path / 'state.db') as db:
        with db._read_ctx() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'work_launches' in tables
            rows = [dict(r) for r in conn.execute('SELECT * FROM work_launches')]
        assert len(rows) == 2
        parent = next(r for r in rows if r['kind'] == 'check_in')
        child = next(r for r in rows if r['kind'] == 'check_in_run')
        assert child['backend_ref'] == fire['id']
        assert child['parent_launch_id'] == parent['launch_id']
        assert child['child_session_id'] is None
        executions.mark_execution_running(fire['id'])
        executions.finish_execution(fire['id'], success=True)
        assert db._read_one('SELECT terminal_cause FROM work_launches WHERE launch_id=?',
                            (child['launch_id'],))[0] == 'success'
