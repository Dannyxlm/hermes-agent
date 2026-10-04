"""Side-task metadata survives live-session retirement, without task text."""
import pytest


@pytest.mark.parametrize('event,kind,outcome', [
    ('background.complete', 'background', 'completed'),
    ('btw.complete', 'btw', 'failed'),
])
def test_spawn_captures_admission_identity_and_finishes_before_emit(tmp_path, monkeypatch, event, kind, outcome):
    import contextlib
    from tui_gateway import server
    from tui_gateway.side_task_activity import registry
    session = {'profile_home': str(tmp_path), 'session_key': 'old', 'cwd': str(tmp_path)}
    queued = []
    observed = []
    monkeypatch.setattr(server, '_start_session_work', lambda run, **kw: queued.append(run) or object())
    monkeypatch.setattr(server, '_set_session_context', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_clear_session_context', lambda *a: None)
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda s: contextlib.nullcontext())
    monkeypatch.setattr(server, '_emit', lambda *a: observed.extend(registry.snapshot(tmp_path, ['old'])))
    def body():
        if outcome == 'failed':
            raise RuntimeError('private failure text')
        return 'private result'
    server._spawn_side_agent('r', session, 'task', 'live', event, body)
    assert registry.snapshot(tmp_path, ['old'])[0]['status'] == 'running'
    session['session_key'] = 'new'
    queued[0]()
    assert observed[0]['kind'] == kind
    assert observed[0]['status'] == outcome
    assert registry.snapshot(tmp_path, ['new']) == []
    assert 'private' not in str(observed)


@pytest.mark.parametrize('start', ['refused', 'raised'])
def test_spawn_failure_is_terminal(tmp_path, monkeypatch, start):
    from tui_gateway import server
    from tui_gateway.side_task_activity import registry
    def spawn(*a, **kw):
        if start == 'raised':
            raise RuntimeError('thread failure')
        return None
    monkeypatch.setattr(server, '_start_session_work', spawn)
    session = {'profile_home': str(tmp_path), 'session_key': 'stored'}
    result = server._spawn_side_agent('r', session, 'task', 'live', 'background.complete', lambda: '')
    assert 'error' in result
    assert registry.snapshot(tmp_path, ['stored'])[0]['status'] == 'failed_start'



@pytest.mark.parametrize('event', ['background.complete', 'btw.complete'])
def test_context_setup_failure_is_recorded(tmp_path, monkeypatch, event):
    from tui_gateway import server
    from tui_gateway.side_task_activity import registry
    monkeypatch.setattr(server, '_start_session_work', lambda run, **kw: run() or object())
    def bad_context(*a, **kw):
        raise RuntimeError('context failed')
    monkeypatch.setattr(server, '_set_session_context', bad_context)
    monkeypatch.setattr(server, '_emit', lambda *a: None)
    session = {'profile_home': str(tmp_path), 'session_key': 'stored'}
    server._spawn_side_agent('r', session, 'task', 'live', event, lambda: '')
    assert registry.snapshot(tmp_path, ['stored'])[0]['status'] == 'failed'


def test_preview_is_not_a_side_task(tmp_path, monkeypatch):
    import contextlib
    from tui_gateway import server
    from tui_gateway.side_task_activity import registry
    monkeypatch.setattr(server, '_start_session_work', lambda run, **kw: run() or object())
    monkeypatch.setattr(server, '_set_session_context', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_clear_session_context', lambda *a: None)
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda s: contextlib.nullcontext())
    monkeypatch.setattr(server, '_emit', lambda *a: None)
    session = {'profile_home': str(tmp_path), 'session_key': 'stored', 'cwd': str(tmp_path)}
    server._spawn_side_agent('r', session, 'task', 'live', 'preview.restart.complete', lambda: '')
    assert registry.snapshot(tmp_path, ['stored']) == []


def test_terminal_retention_never_prunes_running(tmp_path):
    from tui_gateway.side_task_activity import SideTaskRegistry
    now = [1]
    registry = SideTaskRegistry(epoch='e', clock=lambda: now[0])
    registry.register(tmp_path, 'p', 'running', 'btw')
    for i in range(55):
        now[0] += 1
        key = registry.register(tmp_path, 'p', str(i), 'background')
        registry.finish(key, 'completed')
    rows = registry.snapshot(tmp_path, ['p'])
    assert len(rows) == 51
    assert {r['task_id'] for r in rows} == {'running', *(str(i) for i in range(5, 55))}
    now[0] += 6*3600 + 1
    assert registry.snapshot(tmp_path, ['p']) == [{
        'task_id': 'running', 'kind': 'btw', 'status': 'running', 'started_at': 1}]


def test_snapshot_is_profile_and_parent_scoped(tmp_path):
    from tui_gateway.side_task_activity import SideTaskRegistry
    registry = SideTaskRegistry(epoch='test-epoch', clock=lambda: 10)
    key = registry.register(tmp_path/'a', 'stored-a', 'bg-1', 'background')
    registry.register(tmp_path/'b', 'stored-a', 'bg-2', 'btw')
    registry.register(tmp_path/'a', 'stored-b', 'bg-3', 'background')
    assert registry.snapshot(tmp_path/'a', ['stored-a']) == [{
        'task_id': 'bg-1', 'kind': 'background', 'status': 'running', 'started_at': 10}]
    registry.finish(key, 'completed')
    rows = registry.snapshot(tmp_path/'a', ['stored-a'])
    assert rows[0]['status'] == 'completed'
    assert rows[0]['finished_at'] == 10
    rows[0]['status'] = 'corrupted'
    assert registry.snapshot(tmp_path/'a', ['stored-a'])[0]['status'] == 'completed'
