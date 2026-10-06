"""An active-only roster cannot certify queued/constructing children absent."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from tests.plugins import test_cloudseed_mobile_activity_batch as fixture

client = fixture.client
workers = fixture.workers
read_batch = fixture.read_batch


@pytest.mark.parametrize('build_error', [None, 'fixture preflight rejection', RuntimeError('fixture')])
def test_constructing_delegate_cannot_clear_workers_and_releases_fence(client, workers, monkeypatch, build_error):
    from tools import delegate_tool as delegate
    c, _home = client
    observed = []

    def constructing(*args, **kwargs):
        row = read_batch(c, 'root').json()['sessions'][0]
        observed.append(row)
        if isinstance(build_error, Exception):
            raise build_error
        return [], build_error

    monkeypatch.setattr(delegate, '_resolve_delegation_credentials', lambda *a: {})
    monkeypatch.setattr(delegate, '_build_children', constructing)
    monkeypatch.setattr(delegate, '_run_batch', lambda *a: '{}')
    if isinstance(build_error, Exception):
        with pytest.raises(RuntimeError, match='fixture'):
            delegate.delegate_task(goal='fixture', parent_agent=SimpleNamespace(session_id='root'))
    else:
        delegate.delegate_task(goal='fixture', parent_agent=SimpleNamespace(session_id='root'))
    assert len(observed) == 1
    assert observed[0]['subagents'] == []
    assert observed[0]['subagents_complete'] is False
    assert observed[0]['processes_complete'] is True
    assert read_batch(c, 'root').json()['sessions'][0]['subagents_complete'] is True


@pytest.mark.parametrize('runner_fails', [False, True])
@pytest.mark.parametrize('home_binding', ['absolute', 'launch', 'expanded'])
def test_async_admission_pending_is_scoped_and_clears_when_settled(client, workers, monkeypatch,
                                                               runner_fails, home_binding):
    import sqlite3
    from hermes_state_schema import SCHEMA_SQL
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools import async_delegation as delegation
    c, home = client
    delegates, _ = workers
    delegates._active_subagents['active'] = {'subagent_id': 'active', 'status': 'running',
        'owner_profile_home': str(home.resolve()), 'owner_conversation_id': 'root'}
    foreign = home / 'profiles' / 'other'
    foreign.mkdir(parents=True)
    (foreign / 'config.yaml').write_text('{}')
    with sqlite3.connect(home / 'state.db') as db:
        db.executescript(SCHEMA_SQL)
        db.executemany('INSERT INTO sessions(id,source,started_at,parent_session_id,end_reason) VALUES(?,\'cli\',1,?,?)',
                       [('root', None, 'compression'), ('tip', 'root', None)])
    pending = []

    def submit(fn):
        future = Future()
        pending.append((fn, future))
        return future

    monkeypatch.setattr(delegation, '_records', {})
    monkeypatch.setattr(delegation, '_get_executor', lambda *a: SimpleNamespace(submit=submit))

    def runner():
        if runner_fails:
            raise RuntimeError('fixture worker failed')
        return {'results': [{'task_index': 0, 'status': 'completed', 'summary': ''}]}

    monkeypatch.setenv('HERMEX_TEST_PROFILE_HOME', str(home))
    bound_home = {'absolute': home, 'launch': None, 'expanded': '$HERMEX_TEST_PROFILE_HOME'}[home_binding]
    token = set_hermes_home_override(bound_home)
    try:
        receipt = delegation.dispatch_async_delegation_batch(
            goals=['fixture'], context=None, toolsets=None, role='leaf', model='fixture',
            session_key='routing-key', parent_session_id='root',
            runner=runner)
    finally:
        reset_hermes_home_override(token)
    try:
        assert receipt['status'] == 'dispatched'
        row = read_batch(c, 'tip').json()['sessions'][0]
        assert row['subagents'] == [{'subagent_id': 'active', 'status': 'running'}]
        assert row['subagents_complete'] is False
        assert read_batch(c, 'unrelated').json()['sessions'][0]['subagents_complete'] is True
        assert read_batch(c, 'root', profile='other').json()['sessions'][0]['subagents_complete'] is True
    finally:
        for fn, future in pending:
            fn()
            future.set_result(None)
    assert read_batch(c, 'tip').json()['sessions'][0]['subagents_complete'] is True
    delegates._active_subagents.clear()
    assert read_batch(c, 'tip').json()['sessions'][0]['subagents'] == []
