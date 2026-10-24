"""Execute the selected schema-31 predecessor, not a reconstruction of its SQL.

The historical modules are imported in isolation from ``git show <pinned-sha>:<path>``.
A shallow checkout without that object explicitly skips; release probes require it.
"""
import importlib
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tests.hermes_state.test_downgrade_delete_guard import _ledger_counts, _settled
import hermes_state_runtime as rt

PRIOR_REVISION = '69244faa516e34e70797b04fefedd3f2d40178ee'


def export_predecessor(destination):
    root = Path(__file__).resolve().parents[2]
    names = subprocess.run(['git', 'ls-tree', '--name-only', PRIOR_REVISION], cwd=root,
                           capture_output=True, text=True, check=True).stdout.splitlines()
    destination.mkdir()
    for name in names:
        if name == 'hermes_state.py' or (name.startswith('hermes_state_') and name.endswith('.py')):
            source = subprocess.run(['git', 'show', f'{PRIOR_REVISION}:{name}'], cwd=root,
                                    capture_output=True, check=True).stdout
            (destination / name).write_bytes(source)
    assert (destination / 'hermes_state.py').is_file()
    return destination


@contextmanager
def predecessor_modules(source):
    def state_names():
        return [name for name in sys.modules if name == 'hermes_state' or name.startswith('hermes_state_')]
    saved = {name: sys.modules.pop(name) for name in state_names()}
    sys.path.insert(0, str(source))
    try:
        module = importlib.import_module('hermes_state')
        assert Path(module.__file__).parent == source
        yield module
    finally:
        for name in state_names():
            del sys.modules[name]
        sys.modules.update(saved)
        sys.path.remove(str(source))


def _phase(db, side):
    return {'side': side, 'schema_version': int(db._read_one(
        "SELECT version FROM schema_version")[0]), 'quick_check': db._read_one('PRAGMA quick_check')[0]}


def round_trip(path, predecessor, origin):
    phases = []
    if origin == 'prior':
        with predecessor_modules(predecessor) as prior, closing(prior.SessionDB(path)) as db:
            db.create_session('keep', source='cli')
            db.append_message('keep', role='user', content='initial')
            phases.append(_phase(db, 'prior'))
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
    with predecessor_modules(predecessor) as prior, closing(prior.SessionDB(path)) as db:
        assert [m['content'] for m in db.get_messages('keep')] == ['initial', 'candidate']
        db.append_message('keep', role='user', content='rollback')
        assert db.delete_session('delete-me')
        assert db.prune_sessions(older_than_days=None, source='rollback-probe') == 1
        assert db.get_session('delete-me') is None and db.get_session('prune-me') is None
        assert _ledger_counts(db, 'delete-me') == (0, 0, 0)
        assert _ledger_counts(db, 'prune-me') == (0, 0, 0)
        # Classify the actual trigger refusal with the predecessor's own error classifier.
        classifier = importlib.import_module('hermes_state_errors').classify_persistence_error
        for delete in (lambda: db.delete_session('busy'), lambda: db.prune_sessions(
                older_than_days=None, source='rollback-active-probe')):
            with pytest.raises(sqlite3.IntegrityError, match='unresolved gateway runtime work') as refused:
                delete()
            assert classifier(refused.value) == 'unknown'
            assert len(db.get_messages('busy')) == 1
            assert _ledger_counts(db, 'busy') == (2, 1, 1)
        phases.append(_phase(db, 'prior') | {'candidate_runtime_history_delete_ok': True,
                                          'candidate_runtime_history_prune_ok': True,
                                          'active_history_protected': True})
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
