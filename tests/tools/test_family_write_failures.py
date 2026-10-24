"""Optional Family metadata must not change the authoritative work outcome."""
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


def unavailable(*args, **kwargs):
    raise sqlite3.OperationalError('fixture launch storage unavailable')


@contextmanager
def unavailable_writer():
    raise RuntimeError('fixture worker ledger unbound')
    yield


def test_process_spawn_survives_unavailable_metadata(tmp_path, monkeypatch):
    from tools import process_registry as pr
    import hermes_state_launch_context
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    monkeypatch.setattr(hermes_state_launch_context, 'launch_writer', unavailable_writer)
    registry = pr.ProcessRegistry()
    started = []
    def spawn(session, *args):
        started.append(session.id)
        return session
    monkeypatch.setattr(registry, '_spawn_local_reserved', spawn)
    result = None
    try:
        result = registry.spawn_local('echo fixture', cwd=str(tmp_path))
    except Exception:
        pass
    assert result is not None and started == [result.id], 'metadata prevented or duplicated the spawn'


def test_process_recording_happens_after_spawn(tmp_path, monkeypatch):
    from tools import process_registry as pr, process_launches
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    order = []
    monkeypatch.setattr(process_launches, 'record_process_launch', lambda session: order.append('metadata'))
    monkeypatch.setattr(registry, '_spawn_local_reserved', lambda session, *args: (order.append('spawn'), session)[1])
    registry.spawn_local('echo fixture', cwd=str(tmp_path))
    assert order == ['spawn', 'metadata']


@pytest.mark.parametrize('failure_at', ['reserve', 'finish'])
def test_delegate_keeps_success_and_complete_event(tmp_path, monkeypatch, failure_at):
    from run_agent import AIAgent
    from tools import delegate_tool as dt, delegate_tool_config as cfg
    import hermes_state_launches
    monkeypatch.setenv('HERMES_SINGLE_QUERY_SESSION', '1')
    monkeypatch.setattr(dt, '_load_config', dict)
    monkeypatch.setattr(cfg, '_load_config', dict)
    monkeypatch.setattr(dt, '_get_worktree_isolation', lambda: False)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **kw: dict(
        model='fixture', provider='openrouter', base_url='https://openrouter.ai/api/v1',
        api_key='fixture', api_mode='chat_completions', command=None, args=None))
    events, ran = [], []
    with SessionDB(tmp_path / 'state.db') as db:
        parent = AIAgent(session_id='parent', session_db=db, model='fixture', provider='openrouter',
                         api_key='fixture', base_url='https://openrouter.ai/api/v1', quiet_mode=True,
                         skip_memory=True, skip_context_files=True, enabled_toolsets=['file'],
                         tool_progress_callback=lambda event, *a, **kw: events.append(event))
        parent._ensure_db_session()
        def conversation(child, **kw):
            child._ensure_db_session()
            ran.append(child.session_id)
            return dict(final_response='real child result', completed=True, api_calls=0, messages=[])
        monkeypatch.setattr(AIAgent, 'run_conversation', conversation)
        if failure_at == 'reserve':
            monkeypatch.setattr(hermes_state_launches, 'record_launch', unavailable)
        else:
            monkeypatch.setattr(SessionDB, 'finish_launch', unavailable)
        result = None
        try:
            result = json.loads(dt.delegate_task(tasks=[{'goal': 'fixture'}], parent_agent=parent))
        except Exception:
            pass
        finally:
            parent.close()
        assert len(ran) == 1 and 'subagent.complete' in events, result
        assert 'real child result' in json.dumps(result), result


def test_cron_save_and_execution_survive_projection_failure(tmp_path, monkeypatch):
    from cron import jobs, executions
    import hermes_state_launch_context
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(hermes_state_launch_context, 'launch_writer', unavailable_writer)
    result = None
    try:
        job = jobs.create_job('fixture', 'every 2h')
        jobs.pause_job(job['id'])
        jobs.resume_job(job['id'])
        jobs.advance_next_runs([job['id']])
        run = executions.create_execution(job['id'], source='scheduler')
        result = executions.finish_execution(run['id'], success=True)
    except Exception:
        pass
    assert result is not None and result['status'] == 'completed', 'persisted work looked failed'
    assert len(jobs.load_jobs()) == 1


def test_disconnect_is_not_restart(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('child', 'cli')
        db.record_launch('launch', kind='lane', launcher_profile='default', launcher_session_id='parent',
                         child_profile='default')
        db.bind_launch('launch', child_session_id='child')
        db.end_session('child', 'ws_orphan_reap')
        assert db._read_one('SELECT terminal_cause FROM work_launches')[0] == 'unknown'


@pytest.mark.parametrize('values', [dict(HERMES_LAUNCH_TITLE=''), dict(HERMES_LAUNCH_KIND='teamate')])
def test_invalid_cli_provenance_is_ignored(monkeypatch, values):
    from hermes_cli import launch_provenance as lp
    lp._pending.clear()
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    result = None
    try:
        result = lp.take_launch()
    except ValueError:
        pass
    assert result == {}, 'optional provenance aborted CLI startup'


def test_profile_not_inferred_from_child_home(tmp_path, monkeypatch):
    from hermes_cli import launch_provenance as lp
    lp._pending.clear()
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profiles' / 'teammate'))
    monkeypatch.setenv('HERMES_STARTED_BY_SESSION_ID', 'parent')
    assert 'started_by_session' not in lp.take_launch()


def test_multiturn_launch_stays_open_after_turn(tmp_path):
    from hermes_state_runtime import (admit_session_input, begin_runtime_epoch, claim_session_input,
                                      settle_session_input)
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('child', 'cli')
        db.record_launch('launch', kind='lane', launcher_profile='default', launcher_session_id='parent',
                         child_profile='default')
        db.bind_launch('launch', child_session_id='child')
        epoch = begin_runtime_epoch(db, instance_id='fixture')
        admit_session_input(db, epoch=epoch, principal_id='fixture', session_id='child', request_id='turn', payload={'text': 'fixture'})
        admission = claim_session_input(db, epoch=epoch, session_id='child')
        settle_session_input(db, epoch=epoch, admission_id=admission['admission_id'],
                             generation=admission['generation'], outcome='completed', result={'completed': True})
        assert db._read_one('SELECT finished_at FROM work_launches')[0] is None
        db.end_session('child', 'user_stop')
        assert db._read_one('SELECT terminal_cause FROM work_launches')[0] == 'user_stop'


def test_cron_only_updates_changed_launches_in_one_transaction(tmp_path, monkeypatch):
    from cron import jobs
    import hermes_state_launch_context
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with SessionDB(tmp_path / 'state.db') as db:
        depths = []
        @contextmanager
        def writer():
            depths.append(getattr(jobs._jobs_lock_state, 'depth', 0))
            yield db
        monkeypatch.setattr(hermes_state_launch_context, 'launch_writer', writer)
        first = jobs.create_job('first', 'every 2h')
        jobs.create_job('second', 'every 3h')
        statements = []
        db._conn.set_trace_callback(statements.append)
        jobs.pause_job(first['id'])
        db._conn.set_trace_callback(None)
        writes = [s for s in statements if s.startswith('UPDATE work_launches')]
        begins = [s for s in statements if s.startswith('BEGIN IMMEDIATE')]
        assert len(begins) == 1 and len(writes) == 1, statements
        assert depths and set(depths) == {0}


def test_cron_execution_does_not_reload_job_store(tmp_path, monkeypatch):
    from cron import jobs, executions
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    job = jobs.create_job('fixture', 'every 2h')
    run = executions.create_execution(job['id'], source='scheduler')
    monkeypatch.setattr(jobs, 'get_job', unavailable)
    finished = None
    try:
        finished = executions.finish_execution(run['id'], success=True)
    except Exception:
        pass
    assert finished is not None and finished['status'] == 'completed'



def test_failed_worker_launch_does_not_poison_transcript_outbox(tmp_path):
    from tests.hermes_state.test_runtime_worker_lifecycle import setup_store
    db, store, _scope = setup_store(tmp_path)
    original = store.rpc
    def failed_metadata(method, **params):
        if params['operation'].startswith('launch.'):
            raise RuntimeError('fixture optional RPC refusal')
        return original(method, **params)
    store.rpc = failed_metadata
    try:
        try:
            store.record_launch('optional', kind='process', launcher_profile='fixture',
                                launcher_session_id='owned', child_profile='fixture')
        except Exception:
            pass
        saved = False
        try:
            store.append_messages_batch('owned', [{'role': 'assistant', 'content': 'real result'}])
            saved = True
        except Exception:
            pass
        assert saved and not store.journal['pending'] and store.failure is None
        assert db.get_messages_as_conversation('owned')[0]['content'] == 'real result'
    finally:
        store.close()
        db.close()


@pytest.mark.asyncio
async def test_background_runs_when_launch_reservation_fails(tmp_path, monkeypatch):
    import asyncio
    import threading
    from gateway import session_tui_side as side
    from gateway.session_controls import AuthorityConnection
    from tests.gateway.test_local_source_labels import _authority
    import hermes_state_launches
    authority, _ = await _authority(tmp_path, monkeypatch)
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    done, ran = threading.Event(), []
    def build(authority, policy, live, task_id, **kwargs):
        ran.append(task_id)
        return SimpleNamespace(run_conversation=lambda **kw: {'completed': True, 'final_response': 'real result'},
                               close=lambda: done.set())
    monkeypatch.setattr(side, '_side_agent', build)
    try:
        created = await connection.dispatch({'id': 1, 'method': 'session.create',
            'params': dict(request_id='optional-bg', source='cli', cwd=str(tmp_path))})
        sid = created['result']['stored_session_id']
        monkeypatch.setattr(hermes_state_launches, 'record_launch', unavailable)
        spawned = await connection.dispatch({'id': 2, 'method': 'prompt.background',
            'params': dict(session_id=sid, text='fixture')})
        assert 'result' in spawned, spawned
        assert await asyncio.to_thread(done.wait, 5)
        assert ran == [spawned['result']['task_id']]
    finally:
        await connection.close()
        authority.db.close()


@pytest.mark.asyncio
async def test_resume_metadata_conflict_keeps_original_row(tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from tests.gateway.test_local_source_labels import _authority
    authority, _ = await _authority(tmp_path, monkeypatch)
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    try:
        created = await connection.dispatch({'id': 1, 'method': 'session.create',
            'params': dict(request_id='resume-fixture', source='cli', cwd=str(tmp_path))})
        fields = dict(session_id=created['result']['stored_session_id'], launch_id='fixed',
                      started_by_session='parent', started_by_profile='default')
        for i, title in enumerate(('original', 'different'), 2):
            result = await connection.dispatch({'id': i, 'method': 'session.resume',
                'params': dict(fields, launch_title=title)})
            assert 'result' in result, result
        row = authority.db._read_one('SELECT title,child_session_id FROM work_launches WHERE launch_id=?', ('fixed',))
        assert tuple(row) == ('original', fields['session_id'])
    finally:
        await connection.close()
        authority.db.close()


def test_batch_names_are_capped_at_the_producer():
    from tools.delegate_launches import launch_metadata
    assert launch_metadata(batch_name='x' * 200)['batch_name'] == 'x' * 39 + '…'



def test_worker_process_launch_uses_compression_lineage(tmp_path):
    from tests.hermes_state.test_runtime_worker_lifecycle import setup_store
    db, store, _scope = setup_store(tmp_path)
    try:
        db.end_session('owned', 'compression')
        db.create_session('continuation', 'cli', parent_session_id='owned', profile_name='fixture')
        recorded = None
        try:
            recorded = store.record_launch('continued-process', kind='process', launcher_profile='fixture',
                launcher_session_id='continuation', child_profile='fixture', backend_ref='process-ref')
        except Exception:
            pass
        assert recorded == 'continued-process'
        row = db._read_one('SELECT launcher_session_id FROM work_launches WHERE launch_id=?', ('continued-process',))
        assert row[0] == 'continuation'
    finally:
        store.close()
        db.close()
