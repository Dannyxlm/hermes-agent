"""Owner control receipts preserve stop intent into managed child interrupt propagation."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('cause', ['user_stop', 'parent_stop', 'owner_restart'])
def test_managed_stop_carries_typed_cause_to_agent(monkeypatch, cause):
    from agent import managed_worker
    monkeypatch.setattr(managed_worker.threading.Thread, "start", lambda self: None)
    frames = iter([{'type': 'stop', 'terminal_cause': cause}, {'type': 'finish'}])
    monkeypatch.setattr(managed_worker, 'read_frame', lambda stream: next(frames))
    interrupted = []
    agent = SimpleNamespace(hard_interrupt=lambda: interrupted.append(True))
    controls = managed_worker.WorkerControls(SimpleNamespace(), 'fixture-route')
    controls.agent = agent
    controls.read()
    assert agent._launch_stop_cause == cause
    assert interrupted == [True]
    assert controls.stopped.is_set()


def test_managed_stop_rejects_success_as_a_stop_cause(monkeypatch):
    from agent import managed_worker
    monkeypatch.setattr(managed_worker.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(managed_worker, 'read_frame', lambda stream: {'type': 'stop', 'terminal_cause': 'success'})
    agent = SimpleNamespace(hard_interrupt=lambda: None)
    controls = managed_worker.WorkerControls(SimpleNamespace(), 'fixture-route')
    controls.agent = agent
    controls.read()
    assert agent._launch_stop_cause == 'unknown'
    assert controls.finish.is_set()
