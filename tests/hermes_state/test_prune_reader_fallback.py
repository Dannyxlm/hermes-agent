"""A reader fallback must not keep the store mutex while prune starts its writer."""
import os
import subprocess
import sys
from pathlib import Path


def test_bounded_prune_releases_reader_fallback_before_writing(tmp_path):
    # Keep a regression deadlock inside a killable child rather than hanging the test runner.
    script = '''
import sys
from pathlib import Path
from hermes_state import SessionDB
from tests.hermes_state.test_prune_bounded_batches import ended
with SessionDB(Path(sys.argv[1])) as db:
    ended(db, 'old')
    db._checkout_read_conn = lambda: None
    result = db.prune_sessions(older_than_days=None, message_cap=2)
    assert result['deleted_ids'] == ['old'], result
    db.set_meta('after-prune', 'writable')
    assert db.get_meta('after-prune') == 'writable'
print('deleted and writable')
'''
    result = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path / 'state.db')],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, 'HERMES_HOME': str(tmp_path)},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'deleted and writable'
