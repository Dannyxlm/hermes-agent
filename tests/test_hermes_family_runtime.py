"""Only resident, profile-qualified objects can prove Family runtime state."""
import sys
import threading
from types import SimpleNamespace

import pytest

from hermes_family import status_for
from hermes_family_runtime import resident_signals


@pytest.mark.parametrize("reason,source,code,scope_pending,expected", [
    ("exited", "", 0, False, "done"), ("exited", "", 3, False, "failed"),
    ("killed", "process.kill", -9, False, "stopped"), ("killed", "process.stop", -9, False, "stopped"),
    ("killed", "backend_lost", -9, False, "unknown"), ("lost", "", None, False, "unknown"),
    ("failed_start", "failed_start", None, False, "failed"), ("killed", "process.kill", -9, True, "unknown")])
def test_process_receipts_do_not_infer_restart_or_stop(tmp_path, monkeypatch, reason, source, code, scope_pending, expected):
    process = SimpleNamespace(id="process", owner_profile_home=str(tmp_path), command="fixture command",
        exited=True, detached=False, completion_reason=reason, termination_source=source, exit_code=code,
        exited_at=100, _heartbeat_last=90, _scope_stop_pending=scope_pending)
    registry = SimpleNamespace(_lock=threading.Lock(), _running={}, _finished={"process": process})
    monkeypatch.setitem(sys.modules, "tools.process_registry", SimpleNamespace(process_registry=registry))
    signals = resident_signals({"default": tmp_path})
    signal = signals[("default", "process")]
    assert status_for({"kind": "process"}, signal, 100) == expected
    assert signal["command"] == "fixture command" and signal["exit_code"] == code
    assert signal["can_stop"] is False


def test_resident_children_match_store_not_same_session_id(tmp_path, monkeypatch):
    from hermes_family import status_for
    homes = {"default": tmp_path / "a", "iris": tmp_path / "b"}
    agents = [SimpleNamespace(session_id="same", _session_db=SimpleNamespace(db_path=path / "state.db"))
              for path in homes.values()]
    registry = SimpleNamespace(_active_subagents_lock=threading.Lock(), _active_subagents={
        str(i): {"agent": agent, "status": "running"} for i, agent in enumerate(agents)})
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_registry", registry)
    monkeypatch.delitem(sys.modules, "tools.process_registry", raising=False)
    signals = resident_signals(homes, can_stop_helper=lambda agent: agent is agents[0])
    assert set(signals) == {("default", "same"), ("iris", "same")}
    assert signals[("default", "same")]["can_stop"] is True
    assert signals[("iris", "same")]["can_stop"] is False
    assert status_for({"kind": "helper", "last_activity_at": 0}, signals[("default", "same")], 600) == "quiet"
    assert set(resident_signals({"default": homes["default"]})) == {("default", "same")}
