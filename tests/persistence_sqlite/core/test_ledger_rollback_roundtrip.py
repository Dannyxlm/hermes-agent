"""Execute the selected schema-31 predecessor, not a reconstruction of its SQL.

The historical tree is exported from the pinned SHA and runs in separate processes.
A shallow checkout without that object explicitly skips; release probes require it.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from pathlib import Path

import pytest

from tests.persistence_sqlite.core._fork_store import SessionDB
from tests.persistence_sqlite.housekeeping.test_downgrade_delete_guard import _ledger_counts, _settled
from tests.persistence_sqlite.core import _fork_runtime as rt

PRIOR_REVISION = '69244faa516e34e70797b04fefedd3f2d40178ee'


def export_predecessor(destination):
    """Use the predecessor's own dependencies, not a mix of old and new imports."""
    root = Path(__file__).resolve().parents[3]
    env = os.environ | {'GIT_OPTIONAL_LOCKS': '0'}
    subprocess.run(['git', 'cat-file', '-e', PRIOR_REVISION + '^{commit}'], cwd=root, env=env, check=True)
    destination.mkdir()
    archive = destination.with_suffix('.tar')
    try:
        subprocess.run(['git', 'archive', '--format=tar', f'--output={archive}', PRIOR_REVISION],
                       cwd=root, env=env, check=True)
        with tarfile.open(archive) as snapshot:
            snapshot.extractall(destination, filter='data')
    finally:
        archive.unlink(missing_ok=True)
    return destination


def run_predecessor(source, path, *, seed):
    verb = 'seed' if seed else 'rollback'
    code = _PRIOR_DRIVER + '\n' + _PRIOR_COUNTS
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    env.update(PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0')
    proc = subprocess.run([sys.executable, '-B', '-c', code, str(path), verb], cwd=source, env=env,
                          capture_output=True, text=True, timeout=45)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_PRIOR_DRIVER = """
from contextlib import closing
from pathlib import Path
import json, sqlite3, sys
import hermes_state
from persistence.sqlite._state.errors import classify_persistence_error
assert Path(hermes_state.__file__).parent == Path.cwd()
"""

_PRIOR_COUNTS = """
def counts(db, sid):
    return tuple(db._read_one('SELECT '
        '(SELECT COUNT(*) FROM session_admissions WHERE target_session_id=?), '
        '(SELECT COUNT(*) FROM worker_executions WHERE session_id=?), '
        '(SELECT COUNT(*) FROM worker_receipts WHERE execution_id IN '
        '(SELECT execution_id FROM worker_executions WHERE session_id=?))', (sid, sid, sid)))
with closing(hermes_state.SessionDB(Path(sys.argv[1]))) as db:
    if sys.argv[2] == 'seed':
        db.create_session('keep', source='cli')
        db.append_message('keep', role='user', content='initial')
        extra = {}
    else:
        assert [m['content'] for m in db.get_messages('keep')] == ['initial', 'candidate']
        db.append_message('keep', role='user', content='rollback')
        assert db.delete_session('delete-me')
        assert db.prune_sessions(older_than_days=None, source='rollback-probe') == 1
        for sid in ('delete-me', 'prune-me'):
            assert db.get_session(sid) is None and counts(db, sid) == (0, 0, 0)
        for delete in (lambda: db.delete_session('busy'), lambda: db.prune_sessions(
                older_than_days=None, source='rollback-active-probe')):
            try:
                delete()
            except sqlite3.IntegrityError as refused:
                assert 'unresolved gateway runtime work' in str(refused)
                assert classify_persistence_error(refused) == 'unknown'
            else:
                raise AssertionError('unresolved work was not protected')
            assert len(db.get_messages('busy')) == 1
            assert counts(db, 'busy') == (2, 1, 1)
        extra = dict(candidate_runtime_history_delete_ok=True, candidate_runtime_history_prune_ok=True,
                     active_history_protected=True)
    print(json.dumps(dict(side='prior', schema_version=int(db._read_one('SELECT version FROM schema_version')[0]),
                          quick_check=db._read_one('PRAGMA quick_check')[0], **extra)))
"""


def _phase(db, side):
    return {'side': side, 'schema_version': int(db._read_one(
        "SELECT version FROM schema_version")[0]), 'quick_check': db._read_one('PRAGMA quick_check')[0]}


def round_trip(path, predecessor, origin):
    phases = []
    if origin == 'prior':
        phases.append(run_predecessor(predecessor, path, seed=True))
    else:
        with closing(SessionDB(path)) as db:
            db.create_session('keep', source='cli')
            db.append_message('keep', role='user', content='initial')
            phases.append(_phase(db, 'candidate'))
    with closing(SessionDB(path)) as db:
        assert [m['content'] for m in db.get_messages('keep')] == ['initial']
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        for sid in ('delete-me', 'prune-me'):
            _settled(db, epoch, sid)
            db.end_session(sid, 'done')
        _settled(db, epoch, 'busy', source='rollback-active-probe')
        db.end_session('busy', 'done')
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='busy',
                               request_id='pending', payload={'text': 'pending'})
        db.append_message('keep', role='assistant', content='candidate')
        phases.append(_phase(db, 'candidate') | {'ledger_rows_per_target': [1, 1, 1]})
    phases.append(run_predecessor(predecessor, path, seed=False))
    with closing(SessionDB(path)) as db:
        assert [m['content'] for m in db.get_messages('keep')] == ['initial', 'candidate', 'rollback']
        db.append_message('keep', role='assistant', content='resumed')
        assert db.get_session('delete-me') is None and db.get_session('prune-me') is None
        assert _ledger_counts(db, 'delete-me') == (0, 0, 0)
        assert _ledger_counts(db, 'prune-me') == (0, 0, 0)
        assert not db._read_one('PRAGMA foreign_key_check')
        phase = _phase(db, 'candidate')
        assert phase['quick_check'] == 'ok'
        phases.append(phase | {'foreign_key_check': 'ok', 'all_writes_read': True})
    return phases


@pytest.mark.parametrize('origin', ['prior', 'candidate'])
def test_selected_predecessor_can_delete_and_prune_after_candidate(tmp_path, origin):
    try:
        predecessor = export_predecessor(tmp_path / 'predecessor')
    except subprocess.CalledProcessError:
        pytest.skip(f'Historical integration {PRIOR_REVISION} is required (unavailable in shallow checkout)')
    phases = round_trip(tmp_path / 'state.db', predecessor, origin)
    assert all(phase['quick_check'] == 'ok' for phase in phases)
