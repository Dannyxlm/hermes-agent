"""Worker display provenance survives parent removal and checkpoint recovery."""
import subprocess
import sys

from tools.process_registry import ProcessRegistry


def test_process_checkpoint_retains_profile_and_conversation(tmp_path, monkeypatch):
    from gateway.session_context import scoped_current_session_id
    import tools.process_registry as module
    registry = ProcessRegistry()
    monkeypatch.setattr(module, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    with scoped_current_session_id('stored'):
        process = registry._new_session('fixture', 'root', 'root', 'route', str(tmp_path))
    process.pid = 123
    registry._running[process.id] = process
    registry._write_checkpoint()
    restored = ProcessRegistry()
    monkeypatch.setattr(restored, '_detached_host_fate', lambda *args: 'running')
    assert restored.recover_from_checkpoint() == 1
    copied = restored._running[process.id]
    assert copied.owner_profile_home == process.owner_profile_home
    assert copied.owner_conversation_id == 'stored'


def test_command_exit_clears_metadata_without_consuming_notification(tmp_path, monkeypatch):
    import tools.process_registry as module
    from tools.process_registry_activity import process_activity_snapshot
    registry = ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL) as command:
        process = registry._new_session('fixture', 'root', 'root', 'stored', str(tmp_path))
        process.process = command
        registry._running[process.id] = process
        registry.completion_queue.put({'fixture': True})
        try:
            assert process_activity_snapshot()[0]['session_id'] == process.id
        finally:
            command.stdin.write(b'x')
            command.stdin.flush()
            command.wait(timeout=10)
        assert process_activity_snapshot() == []
        assert not process.exited
        assert registry.completion_queue.get_nowait() == {'fixture': True}
