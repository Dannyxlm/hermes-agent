"""Checkpoint adoption resumes process heartbeats without requiring a new spawn."""

import json
import time

import pytest

from tools import process_registry as pr
from tools.process_registry_output_log import output_dir


@pytest.mark.parametrize("log_backed", [False, True])
def test_recovered_heartbeat_emits_recovered_then_new_output(tmp_path, monkeypatch, log_backed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "HEARTBEAT_MIN_SECONDS", 1)
    monkeypatch.setattr(pr, "HEARTBEAT_TICK_SECONDS", 0.02)
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    registry = pr.ProcessRegistry()
    # PID identity is independent of this regression; no process is spawned or signalled.
    monkeypatch.setattr(registry, "_detached_host_fate", lambda *_: "running")
    log = output_dir() / "proc_recovered.log"
    entry = {"session_id": "proc_recovered", "command": "long-job", "pid": 123,
             "pid_scope": "host", "host_start_time": 1, "heartbeat_seconds": 1,
             "notify_on_complete": True, "started_at": time.time() - 100}
    if log_backed:
        log.parent.mkdir(parents=True)
        log.write_text("before recovery\n")
        entry["output_log"] = str(log)
    checkpoint.write_text(json.dumps([entry]))
    session = None
    started = time.time()
    try:
        assert registry.recover_from_checkpoint() == 1
        session = registry.get("proc_recovered")
        assert session is not None and session.heartbeat_seconds == 1
        if not log_backed:
            registry._ingest_output(session, "before recovery\n")

        first = registry.completion_queue.get(timeout=10)
        assert session._heartbeat_last >= started
        assert first["type"] == "heartbeat" and first["seq"] == 1
        assert first["output"] == "before recovery\n"
        assert first["session_id"] == session.id
        timer = registry._heartbeat_thread
        assert timer is not None and timer.is_alive()
        assert registry.recover_from_checkpoint() == 0
        assert registry._heartbeat_thread is timer

        if log_backed:
            with log.open("a") as stream:
                stream.write("after recovery\n")
        else:
            registry._ingest_output(session, "after recovery\n")
        second = registry.completion_queue.get(timeout=10)
        assert second["type"] == "heartbeat" and second["seq"] == 2
        assert second["output"] == "after recovery\n"
    finally:
        if session is not None and session._reader_thread is not None:
            log.with_suffix(".exit").write_text("0\n")
            session._reader_finish_requested.set()
            session._reader_thread.join(timeout=5)
            assert not session._reader_thread.is_alive()
        with registry._lock:
            registry._running.clear()


def test_recovery_without_heartbeat_does_not_start_timer(tmp_path, monkeypatch):
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(registry, "_detached_host_fate", lambda *_: "running")
    checkpoint.write_text(json.dumps([{
        "session_id": "proc_quiet", "command": "quiet-job", "pid": 123,
        "pid_scope": "host", "host_start_time": 1, "heartbeat_seconds": 0}]))

    assert registry.recover_from_checkpoint() == 1
    assert registry.get("proc_quiet").heartbeat_seconds == 0
    assert registry._heartbeat_thread is None
    assert registry.completion_queue.empty()
