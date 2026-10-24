"""Tracked process launch metadata exists before execution and survives checkpoints."""
import pytest

from gateway.session_context import scoped_current_session_id
from hermes_state import SessionDB
from tools.process_registry import ProcessRegistry


def test_process_launch_checkpoint_and_completion(tmp_path, monkeypatch):
    import tools.process_registry as module
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(module, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    registry = ProcessRegistry()
    with scoped_current_session_id('parent'):
        process = registry._new_session('a command with a descriptive title ' + 'x' * 60,
                                        'task', 'task', 'route', str(tmp_path))
    assert getattr(process, 'launcher_session_id', None) == 'parent'
    assert process.launcher_profile == 'default'
    assert len(process.title) == 40
    with SessionDB(tmp_path / 'state.db') as db:
        row = db._read_one('SELECT * FROM work_launches WHERE launch_id=?', (process.launch_id,))
        assert row['backend_ref'] == process.id
        assert row['child_session_id'] is None
        assert row['finished_at'] is None
        process.pid = 123
        registry._running[process.id] = process
        registry._write_checkpoint()
        restored = ProcessRegistry()
        monkeypatch.setattr(restored, '_detached_host_fate', lambda *a: 'running')
        assert restored.recover_from_checkpoint() == 1
        copy = restored._running[process.id]
        assert (copy.launch_id, copy.launcher_session_id, copy.title) == (process.launch_id, 'parent', process.title)
        registry._finish_exited(process, 0)
        assert db._read_one('SELECT terminal_cause FROM work_launches WHERE launch_id=?',
                            (process.launch_id,))[0] == 'success'


def test_failed_popen_retains_failed_start_receipt(tmp_path, monkeypatch):
    import tools.process_registry as module
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(module, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    registry = ProcessRegistry()
    def fail(*a, **kw):
        with SessionDB(tmp_path / 'state.db') as db:
            assert db._read_one('SELECT COUNT(*) FROM work_launches')[0] == 1
        raise OSError('fixture start failure')
    monkeypatch.setattr(module.subprocess, 'Popen', fail)
    monkeypatch.setattr(registry, '_scope_argv', lambda *a: ['fixture'])
    with scoped_current_session_id('parent'), pytest.raises(OSError, match='fixture'):
        registry.spawn_local('fixture command', cwd=str(tmp_path))
    with SessionDB(tmp_path / 'state.db') as db:
        assert db._read_one('SELECT terminal_cause FROM work_launches')[0] == 'failed_start'
