"""A real -Q CLI, fake loopback inference, two delegates and cron share exact launch edges."""
import json
from pathlib import Path
import sqlite3
import subprocess

from tests.hermes_cli.test_finite_chat_delegation import test_finite_chat_joins_parallel_children_before_final_response as finite_chat


def test_family_end_to_end(tmp_path, monkeypatch):
    real_run = subprocess.run
    child_launches = []

    def run(command, *args, **kwargs):
        if isinstance(command, list) and 'from hermes_cli.main import main; main()' in command:
            assert '-Q' in command
            env = dict(kwargs['env'])
            home = Path(env['HERMES_HOME'])
            from hermes_state import SessionDB
            with SessionDB(home / 'state.db') as db:
                db.create_session('family-parent', 'cli', profile_name='default')
            env.update(HERMES_STARTED_BY_SESSION_ID='family-parent', HERMES_STARTED_BY_PROFILE='default',
                       HERMES_LAUNCH_TITLE='CLI fan-out child', HERMES_LAUNCH_ID='family-cli-launch')
            kwargs['env'] = env
            child_launches.append(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    finite_chat(tmp_path, 'quiet')
    assert len(child_launches) == 1
    home = tmp_path / 'profile'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SESSION_ID', 'family-parent')
    from gateway.session_context import scoped_current_session_id
    from cron.jobs import create_job
    with scoped_current_session_id('family-parent'):
        job = create_job('Scheduled fixture check-in', 'every 2h', name='Fixture check-in')
    with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('SELECT * FROM work_launches ORDER BY created_at')]
        root = next(r for r in rows if r['launch_id'] == 'family-cli-launch')
        helpers = [r for r in rows if r['kind'] == 'helper']
        scheduled = next(r for r in rows if r['kind'] == 'check_in')
        assert root['launcher_session_id'] == 'family-parent'
        assert root['terminal_cause'] == 'success'
        assert len(helpers) == 2
        assert {r['launcher_session_id'] for r in helpers} == {root['child_session_id']}
        assert len({r['batch_id'] for r in helpers}) == 1
        assert all(r['terminal_cause'] == 'success' for r in helpers)
        assert scheduled['backend_ref'] == job['id']
        assert scheduled['launcher_session_id'] == 'family-parent'
        assert scheduled['next_run_at'] is not None
        assert db.execute("SELECT parent_session_id FROM sessions WHERE id=?", (root['child_session_id'],)).fetchone()[0] is None
        receipt = {'parent': 'family-parent', 'cli_child': root['child_session_id'],
                   'helper_sessions': [r['child_session_id'] for r in helpers],
                   'helper_batch': helpers[0]['batch_id'], 'cron_job': job['id'], 'launch_count': len(rows)}
    (tmp_path / 'family-receipt.json').write_text(json.dumps(receipt, sort_keys=True), encoding='utf-8')
    print('FAMILY_END_TO_END ' + json.dumps(receipt, sort_keys=True))
