"""Restart is a resumable state, not the final receipt for a resumed launch."""
import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt
from hermes_state_local import bind_resumed_launch


FIELDS = dict(kind='lane', launcher_profile='default', launcher_session_id='parent', child_profile='default')


def state(db):
    return tuple(db._read_one('SELECT finished_at,terminal_cause FROM work_launches WHERE launch_id=?', ('launch',)))


@pytest.mark.parametrize('resume', ['bind', 'record', 'local', 'reopen', 'turn'])
@pytest.mark.parametrize('kind', ['lane', 'teammate'])
def test_resume_clears_restart_and_normal_end_records_the_real_cause(tmp_path, resume, kind):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='fixture')
        db.create_session('child', 'cli')
        fields = dict(FIELDS, kind=kind)
        db.record_launch('launch', **fields)
        db.bind_launch('launch', child_session_id='child')
        db.end_session('child', 'startup_orphan_reap')
        assert state(db)[1] == 'owner_restart'
        if resume == 'bind':
            db.bind_launch('launch', child_session_id='child')
        elif resume == 'record':
            db.record_launch('launch', **fields)
        elif resume == 'local':
            bind_resumed_launch(db, epoch=epoch, session_id='child', fields=dict(
                started_by_session='parent', started_by_profile='default', launch_kind=kind, launch_id='launch'))
        elif resume == 'reopen':
            db.reopen_session('child')
        else:
            rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='child',
                                   request_id='turn', payload={'text': 'resume'})
            claimed = rt.claim_session_input(db, epoch=epoch, session_id='child')
            assert claimed['status'] == 'started'
        assert state(db) == (None, None), f'{resume} left a resumed launch paused by restart'
        db.reopen_session('child')
        db.end_session('child', 'user_stop')
        assert state(db)[0] is not None and state(db)[1] == 'user_stop'


@pytest.mark.parametrize('cause', ['success', 'error', 'user_stop', 'parent_stop', 'failed_start'])
def test_rebind_and_new_turn_preserve_concrete_terminal_receipts(tmp_path, cause):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='fixture')
        db.create_session('child', 'cli')
        db.record_launch('launch', **FIELDS)
        db.bind_launch('launch', child_session_id='child')
        db.finish_launch('launch', cause)
        original = state(db)
        db.record_launch('launch', **FIELDS)
        db.bind_launch('launch', child_session_id='child')
        db.reopen_session('child')
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='child',
                               request_id='turn', payload={'text': 'another turn'})
        rt.claim_session_input(db, epoch=epoch, session_id='child')
        assert state(db) == original


def test_rejected_resume_does_not_clear_restart(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('child', 'cli')
        db.record_launch('launch', **FIELDS)
        db.bind_launch('launch', child_session_id='child')
        db.finish_launch('launch', 'owner_restart')
        original = state(db)
        with pytest.raises(ValueError):
            db.bind_launch('launch', child_session_id='foreign')
        with pytest.raises(ValueError):
            db.record_launch('launch', **dict(FIELDS, launcher_session_id='foreign'))
        assert state(db) == original


def test_plain_websocket_orphan_is_unknown_not_owner_restart(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('child', 'cli')
        db.record_launch('launch', **FIELDS)
        db.bind_launch('launch', child_session_id='child')
        db.end_session('child', 'ws_orphan_reap')
        assert state(db)[1] == 'unknown'
