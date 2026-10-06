"""Display-only worker discovery must outlive the parent and preserve profile isolation."""
from types import SimpleNamespace

import pytest

from tests.plugins import test_cloudseed_mobile as mobile_fixture

P = mobile_fixture.P
client = mobile_fixture.client


@pytest.fixture
def workers(monkeypatch):
    from tools import delegate_tool_registry as delegates
    from tools.process_registry import process_registry
    monkeypatch.setattr(delegates, '_active_subagents', {})
    monkeypatch.setattr(process_registry, '_running', {})
    monkeypatch.setattr(process_registry, '_finished', {})
    return delegates, process_registry


def read_batch(c, *ids, profile='default'):
    return c.get(P + '/session-activity/batch', params=[('profile', profile),
                 *(('stored_session_id', sid) for sid in ids)])


def test_batch_discovers_scoped_workers_and_clears_after_last_exit(client, workers):
    from tools.process_registry import ProcessRegistry
    from tools.delegate_tool_child_run import _register_child
    from tui_gateway.side_task_activity import registry
    c, home = client
    delegates, processes = workers
    parent = SimpleNamespace(session_id='root')
    child = SimpleNamespace(_subagent_id='child', session_id='child-session')
    _register_child(child, parent, 'private goal', owner_session_id=None,
                    owner_transport=None, owner_session_record=None)
    grandchild = SimpleNamespace(_subagent_id='grandchild', session_id='grandchild-session')
    _register_child(grandchild, child, 'nested private goal', owner_session_id=None,
                    owner_transport=None, owner_session_record=None)
    proc = ProcessRegistry._new_session('private command', 'grandchild', 'grandchild',
                                        'grandchild-session', str(home))
    processes._running[proc.id] = proc
    registry.register(home, 'root', 'background-one', 'background')
    # No live gateway holder is created. The original parent can already be gone.
    first = read_batch(c, 'root', 'unopened')
    assert first.status_code == 200
    body = first.json()
    row, unopened = body['sessions']
    assert {r['subagent_id'] for r in row['subagents']} == {'child', 'grandchild'}
    assert [r['session_id'] for r in row['processes']] == [proc.id]
    assert row['subagents_complete'] and row['processes_complete'] and row['complete']
    assert unopened['subagents'] == unopened['processes'] == unopened['tasks'] == []
    assert 'private' not in first.text and str(home) not in first.text
    delegates._active_subagents.clear()
    parent_gone = read_batch(c, 'root').json()['sessions'][0]
    assert parent_gone['subagents'] == []
    assert [r['session_id'] for r in parent_gone['processes']] == [proc.id]
    proc.exited = True
    final = read_batch(c, 'root').json()['sessions'][0]
    assert final['subagents'] == final['processes'] == []
    assert final['subagents_complete'] and final['processes_complete']
    assert final['tasks'][0]['status'] == 'running'


def test_batch_cloned_ids_and_compression_do_not_cross_profiles_or_branches(client, workers):
    import sqlite3
    from gateway.session_context import scoped_current_session_id
    from tools.process_registry import ProcessRegistry
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    c, home = client
    delegates, processes = workers
    other = home / 'profiles' / 'other'
    other.mkdir(parents=True)
    (other / 'config.yaml').write_text('{}')
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT)')
        db.executemany('INSERT INTO sessions VALUES(?,?,?)', [
            ('root', None, 'compression'), ('tip', 'root', None), ('branch', 'tip', None)])
    before = (home / 'state.db').read_bytes()
    for directory, label in [(home, 'ours'), (other, 'foreign')]:
        token = set_hermes_home_override(directory)
        try:
            with scoped_current_session_id('root'):
                proc = ProcessRegistry._new_session('private', label, label, 'route', str(directory))
        finally:
            reset_hermes_home_override(token)
        processes._running[proc.id] = proc
        delegates._active_subagents[label] = {'subagent_id': label, 'status': 'running',
            'owner_profile_home': str(directory.resolve()), 'owner_conversation_id': 'root',
            'owner_agent_session_id': 'root', 'started_at': 1}
    row = read_batch(c, 'tip').json()['sessions'][0]
    assert [r['subagent_id'] for r in row['subagents']] == ['ours']
    assert len(row['processes']) == 1
    branch = read_batch(c, 'branch').json()['sessions'][0]
    assert branch['subagents'] == branch['processes'] == []
    foreign = read_batch(c, 'root', profile='other').json()['sessions'][0]
    assert [r['subagent_id'] for r in foreign['subagents']] == ['foreign']
    assert foreign['processes'][0]['session_id'] != row['processes'][0]['session_id']
    assert (home / 'state.db').read_bytes() == before


@pytest.mark.parametrize('durable_id', ['stored-id', ''])
def test_gateway_process_requires_durable_conversation_not_routing_key(client, workers, durable_id):
    from gateway.session_context import scoped_current_session_id
    from tools.process_registry import ProcessRegistry
    c, home = client
    _, processes = workers
    route = 'agent:main:telegram:dm:fixture'
    with scoped_current_session_id(durable_id):
        process = ProcessRegistry._new_session('fixture', route, route, route, str(home))
    processes._running[process.id] = process
    row = read_batch(c, 'stored-id').json()['sessions'][0]
    assert process.parent_session_id == durable_id
    assert [item['session_id'] for item in row['processes']] == ([process.id] if durable_id else [])
    assert row['processes_complete'] is bool(durable_id)


def test_old_unknown_provenance_cannot_authorize_empty_worker_evidence(client, workers):
    from tools.process_registry import ProcessSession
    c, home = client
    delegates, processes = workers
    delegates._active_subagents['old'] = {'subagent_id': 'old', 'status': 'running',
                                         'owner_agent_session_id': 'root'}
    processes._running['old'] = ProcessSession(id='old', command='private', session_key='root')
    row = read_batch(c, 'root').json()['sessions'][0]
    assert row['subagents'] == row['processes'] == []
    assert row['subagents_complete'] is False and row['processes_complete'] is False
    assert row['complete'] is True  # Side-task completeness is independent.


@pytest.mark.parametrize('ids', [[], [''], ['x' * 513], ['bad\x00id'], ['same', 'same'],
                                [str(i) for i in range(51)]])
def test_batch_rejects_missing_malformed_duplicate_or_oversized_ids(client, ids):
    c, _ = client
    assert read_batch(c, *ids).status_code in {400, 422}


def test_batch_snapshot_failure_is_unknown_and_does_not_attach(client, workers, monkeypatch):
    from tui_gateway import server
    from plugins.cloudseed_mobile import session_activity as activity
    c, _ = client
    monkeypatch.setattr(server, '_start_agent_build', lambda *a: pytest.fail('metadata must not build'))
    monkeypatch.setattr(server, '_rebind_live_transport', lambda *a: pytest.fail('metadata must not attach'))
    def unavailable():
        raise RuntimeError('fixture unavailable')
    monkeypatch.setattr(activity, '_process_snapshot', unavailable)
    row = read_batch(c, 'root').json()['sessions'][0]
    assert row['processes'] is None and row['processes_complete'] is False
    assert row['subagents'] == [] and row['subagents_complete'] is True


def test_batch_malformed_domain_preserves_other_domains_and_auth(client, workers, monkeypatch):
    from plugins.cloudseed_mobile import session_activity as activity
    c, _ = client
    monkeypatch.setattr(activity, '_agent_snapshot', lambda: [None])
    row = read_batch(c, 'root').json()['sessions'][0]
    assert row['subagents_complete'] is False
    assert row['processes_complete'] is True
    assert len(read_batch(c, *(str(i) for i in range(50))).json()['sessions']) == 50
    c.headers.pop('Authorization')
    assert read_batch(c, 'root').status_code == 401
