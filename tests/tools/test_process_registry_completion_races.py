"""A process's completion is published once, by its owner, and only with its final output.

Child exit, result finalization, notice publication and output consumption are separate steps.
Each test forces one interleaving where another caller treated an earlier step as the last one.
"""

import shlex
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

import tools.process_registry as module
from tools.process_registry import ProcessRegistry, ProcessSession


def _session(sid: str, **fields) -> ProcessSession:
    return ProcessSession(id=sid, command="cmd", task_id="task", started_at=time.time(), **fields)


@pytest.mark.parametrize('consume', ['wait', 'log'])
def test_consumption_between_snapshot_and_publication_cannot_resurrect_notice(tmp_path, monkeypatch, consume):
    import queue
    from tools import process_registry_results as results
    from tools.process_registry_delivery import restore_process_completions
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(module, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    registry = ProcessRegistry()
    process = ProcessSession(id='proc_consume_race', command='build', started_at=1,
        session_key='owner', parent_session_id='owner', notify_on_complete=True, output_buffer='final output')
    registry._running[process.id] = process
    snapshot, release, observed = (threading.Event() for _ in range(3))
    save = results.save_completed_record

    def paused_save(record):
        if record['notification']['status'] == 'pending':
            snapshot.set()
            assert release.wait(10)
        return save(record)

    monkeypatch.setattr(results, 'save_completed_record', paused_save)
    seen = []

    def read():
        seen.append(registry.wait(process.id, timeout=5) if consume == 'wait' else registry.read_log(process.id))
        observed.set()

    finisher = threading.Thread(target=registry._finish_exited, args=(process, 0))
    # Capture the live session before finalization takes the registry lock.
    original = registry.get
    monkeypatch.setattr(registry, 'get', lambda sid: process if sid == process.id else original(sid))
    finisher.start()
    reader = threading.Thread(target=read)
    try:
        assert snapshot.wait(10)
        reader.start()
        assert observed.wait(5), 'collected output was blocked behind producer publication'
    finally:
        release.set()
        finisher.join(10)
        if reader.ident is not None:
            reader.join(10)
    assert not finisher.is_alive() and not reader.is_alive()
    assert seen and seen[0]['output'] == 'final output'
    replay = queue.Queue()
    restore_process_completions(replay)
    assert replay.empty(), 'a stale producer snapshot resurrected consumed output'


def test_duplicate_finisher_leaves_completion_to_the_owner(monkeypatch):
    """A second finisher must not release waiters while the first is still publishing the notice."""
    registry = ProcessRegistry()
    session = _session("proc_dup_finish", notify_on_complete=True)
    registry._running[session.id] = session
    entered, release = threading.Event(), threading.Event()
    calls = []

    def checkpoint(*_args, **_kwargs):
        calls.append(None)
        if len(calls) == 1:  # the owner, between the registry move and the notice
            entered.set()
            release.wait(10)

    monkeypatch.setattr(registry, "_write_checkpoint", checkpoint)
    owner = threading.Thread(target=registry._move_to_finished, args=(session,), daemon=True)
    owner.start()
    try:
        assert entered.wait(5)
        assert registry._move_to_finished(session) is False
        assert not session._completion_event.is_set(), "duplicate released waiters before the notice"
        assert registry.completion_queue.empty()
    finally:
        release.set()
        owner.join(5)
    assert session._completion_event.is_set()
    assert registry.completion_queue.qsize() == 1


def test_owner_that_fails_mid_publish_still_releases_waiters(monkeypatch):
    registry = ProcessRegistry()
    session = _session("proc_owner_fails", notify_on_complete=True)
    registry._running[session.id] = session

    def checkpoint(*_args, **_kwargs):
        raise RuntimeError("checkpoint write failed")

    monkeypatch.setattr(registry, "_write_checkpoint", checkpoint)
    with pytest.raises(RuntimeError):
        registry._move_to_finished(session)
    assert session._completion_event.is_set()


def test_reader_exit_cannot_overwrite_a_committed_kill(monkeypatch):
    """The reader records its exit under the session lock, so it sees a kill committed under it."""
    registry = ProcessRegistry()
    session = _session("proc_kill_vs_reader")
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *_a, **_k: None)

    reader = threading.Thread(target=registry._finish_exited, args=(session, 0), daemon=True)
    with session._lock:  # kill_process commits its result under this lock
        reader.start()
        time.sleep(0.2)
        recorded_early = session.exited
        session.exited = True
        session.exit_code = -15
        session.completion_reason = "killed"
        session.termination_source = "process.kill"
    reader.join(5)

    assert not recorded_early, "reader recorded its exit without the session lock"
    assert (session.exit_code, session.completion_reason, session.termination_source) == (
        -15, "killed", "process.kill")


def _finalizing_session(registry: ProcessRegistry) -> ProcessSession:
    """Direct child exited and a reconcile flipped ``exited``; the reader is still on its final drain."""
    session = _session("proc_finalizing", notify_on_complete=True)
    session.process = cast(Any, SimpleNamespace(poll=lambda: 0, stdout=None, stderr=None, stdin=None))
    session._reader_selectable = True
    session._reader_thread = cast(Any, SimpleNamespace(is_alive=lambda: True))
    session._reader_finish_requested.set()
    session.mark_exited(0)
    session.append_output("partial")
    registry._running[session.id] = session
    return session


def test_status_reads_do_not_consume_a_completion_still_being_finalized():
    registry = ProcessRegistry()
    session = _finalizing_session(registry)

    assert registry.poll(session.id)["status"] == "exited"
    registry.read_log(session.id)

    assert session.id not in registry._poll_observed
    assert session.id not in registry._completion_consumed


class _ReleaseHookLock:
    """Session lock that runs a hook once, right after its first release."""

    def __init__(self, hook):
        self._lock, self._hook = threading.Lock(), hook

    def acquire(self, *args, **kwargs):
        return self._lock.acquire(*args, **kwargs)

    __enter__ = acquire

    def release(self):
        self._lock.release()
        hook, self._hook = self._hook, None
        if hook:
            hook()

    def __exit__(self, *_exc):
        self.release()


@pytest.mark.parametrize("read", [
    lambda registry, sid: registry.poll(sid),
    lambda registry, sid: registry.read_log(sid),
    lambda registry, sid: registry.kill_process(sid),
], ids=["poll", "read_log", "kill_exited"])
def test_a_snapshot_taken_before_the_reader_finishes_does_not_consume_it(read, monkeypatch):
    """The reader finishes between the snapshot and the consume decision: the decision must
    describe the snapshot returned, so the completion still delivers the whole output."""
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *_a, **_k: None)
    session = _finalizing_session(registry)

    def reader_finishes():
        session.append_output("FINAL")
        registry._finish_exited(session, 0)

    session._lock = cast(Any, _ReleaseHookLock(reader_finishes))
    read(registry, session.id)

    assert session._completion_event.is_set()
    assert session.id not in registry._poll_observed
    assert not registry.is_completion_consumed(session.id)
    assert registry.completion_queue.get_nowait()["output"].endswith("FINAL")


@pytest.mark.platforms("posix")
def test_wait_returns_the_final_tail_when_the_reader_drains_slowly(tmp_path, monkeypatch):
    """``wait`` must not take (and consume) the output before the reader has drained the exited
    child's tail, however long the reader takes."""
    monkeypatch.setattr(module, "_SYSTEMD_SCOPE_AVAILABLE", False, raising=False)
    registry = ProcessRegistry()
    stalled, release = threading.Event(), threading.Event()

    def slow_sink(_session, _chunk):  # a live-output consumer that falls behind
        if not stalled.is_set():
            stalled.set()
            release.wait(10)

    registry.on_output = slow_sink
    burst = ("import sys; sys.stdout.write('x' * 99 + '\\n'); "
             "sys.stdout.write(('y' * 99 + '\\n') * 100 + 'FINAL\\n')")
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} -c {shlex.quote(burst)}", cwd=str(tmp_path))
    session.notify_on_complete = True
    try:
        assert stalled.wait(10)
        assert session.process.wait(timeout=10) == 0
        # Longer than any bounded wait inside the reconcile.
        threading.Timer(2.5, release.set).start()
        result = registry.wait(session.id, timeout=15)
        assert result["status"] == "exited"
        assert result["output"].rstrip().endswith("FINAL"), result["output"][-80:]
    finally:
        release.set()
        registry.kill_all(source="test")


def test_kill_with_a_failed_scope_stop_keeps_the_session_running(monkeypatch):
    """Without a stopped scope, nothing proves the detached descendants are gone."""
    registry = ProcessRegistry()
    session = _session("proc_scope_stuck", systemd_unit="hermes-worker-proc_scope_stuck.scope")
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_signal_kill", lambda *_args: None)
    monkeypatch.setattr(registry, "_post_kill_survivors", lambda _session: [])
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "_stop_systemd_unit", lambda _unit: False)

    result = registry.kill_process(session.id)

    assert result["status"] == "error"
    assert result["process_running"] is True
    assert "hermes-worker-proc_scope_stuck.scope" in result["error"]
    assert session.id in registry._running
    assert not session.exited


def test_kill_of_an_exited_session_reports_a_failed_scope_stop(monkeypatch):
    registry = ProcessRegistry()
    session = _session("proc_exited_scope_stuck", systemd_unit="hermes-worker-proc_exited_scope_stuck.scope")
    session.mark_exited(0)
    registry._finished[session.id] = session
    monkeypatch.setattr(module, "_stop_systemd_unit", lambda _unit: False)

    result = registry.kill_process(session.id)

    assert result["status"] == "already_exited"
    assert result["scope_stop_failed"] == "hermes-worker-proc_exited_scope_stuck.scope"


def test_a_reader_exit_during_a_failed_scope_stop_is_not_reported_as_killed(monkeypatch):
    """The root exiting proves nothing about the scope's detached descendants."""
    registry = ProcessRegistry()
    session = _session("proc_scope_race", systemd_unit="hermes-worker-proc_scope_race.scope")
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_signal_kill", lambda *_args: None)
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *_a, **_k: None)

    def stop_while_the_reader_finishes(_unit):
        registry._finish_exited(session, 0)
        return False

    monkeypatch.setattr(module, "_stop_systemd_unit", stop_while_the_reader_finishes)
    result = registry.kill_process(session.id)

    assert result["status"] == "error"
    assert result["scope_stop_failed"] == "hermes-worker-proc_scope_race.scope"
    assert result["process_running"] is False
    assert (session.completion_reason, session.exit_code) == ("exited", 0)
    assert session.id in registry._finished  # keeps its unit, so a later kill retries the stop


class _SandboxEnv:
    """Non-local backend double: answers the poller's log-delta, liveness and exit-code reads,
    running ``hooks[step]`` first so a test can land a kill at an exact point."""

    def __init__(self, *, delta="", alive=False, hooks=None):
        self.delta, self.alive, self.hooks = delta, alive, hooks or {}

    def execute(self, command, **_kwargs):
        if command.startswith("kill -0"):
            step, out = "check", "0\n" if self.alive else "1\n"
        elif command.startswith("kill "):
            return {"output": ""}
        elif command.startswith("cat "):
            step, out = "exit", "0\n"
        else:
            step, out = "delta", f"{len(self.delta.encode())} 0\n{self.delta}"
        hook = self.hooks.pop(step, None)
        if hook:
            hook()
        return {"output": out}


def _sandbox_session(registry, monkeypatch, env) -> ProcessSession:
    session = _session("proc_sandbox", pid=4242, pid_scope="sandbox", env_ref=env, notify_on_complete=True)
    registry._running[session.id] = session
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(registry, "_post_kill_survivors", lambda _session: [])
    return session


def _poll_sandbox(registry, session, env):
    registry._env_poller_loop(session, env, "/tmp/bg.log", "/tmp/bg.pid", "/tmp/bg.exit")


def test_consumption_is_exact_to_process_start_and_idempotent(monkeypatch, tmp_path):
    from tools.process_registry_delivery import (completion_event, mark_completion_consumed,
                                                 notification_state, restore_process_completions)
    from tools.process_registry_results import save_completed_result

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    older = ProcessSession(id='proc_reused', command='old build', started_at=1, notify_on_complete=True)
    current = ProcessSession(id='proc_reused', command='new build', started_at=2, notify_on_complete=True)
    save_completed_result(current)
    event = completion_event(current)
    mark_completion_consumed(registry, older)
    pending = notification_state(event)
    assert pending is not None and pending['status'] == 'pending'
    mark_completion_consumed(registry, current)
    mark_completion_consumed(registry, current)
    consumed = notification_state(event)
    assert consumed is not None and consumed['reason'] == 'consumed'
    restored = ProcessRegistry()
    restore_process_completions(restored.completion_queue)
    assert restored.completion_queue.empty()


def test_notification_kill_does_not_reenter_the_process_lock(monkeypatch):
    registry = ProcessRegistry()
    env = _SandboxEnv()
    session = _sandbox_session(registry, monkeypatch, env)
    session.notify_on_complete = True

    class BoundedLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            assert self.lock.acquire(timeout=2), 'process lock reentered during notification publication'

        def __exit__(self, *args):
            self.lock.release()

    session._lock = cast(Any, BoundedLock())
    result = registry.kill_process(session.id)
    assert result['status'] == 'killed', result
    assert registry.is_completion_consumed(session.id)


def test_a_sandbox_exit_read_after_a_kill_keeps_the_kill(monkeypatch):
    registry = ProcessRegistry()
    killed = []
    env = _SandboxEnv(hooks={"exit": lambda: killed.append(registry.kill_process("proc_sandbox"))})
    session = _sandbox_session(registry, monkeypatch, env)

    _poll_sandbox(registry, session, env)

    assert killed[0]["status"] == "killed"
    assert (session.exit_code, session.completion_reason, session.termination_source) == (
        -15, "killed", "process.kill")


def test_a_sandbox_lost_after_a_kill_keeps_the_kill(monkeypatch):
    registry = ProcessRegistry()

    def kill_then_lose_the_backend():
        registry.kill_process("proc_sandbox")
        raise RuntimeError("sandbox reaped")

    env = _SandboxEnv(alive=True, hooks={"check": kill_then_lose_the_backend})
    session = _sandbox_session(registry, monkeypatch, env)

    _poll_sandbox(registry, session, env)

    assert (session.exit_code, session.completion_reason, session.termination_source) == (
        -15, "killed", "process.kill")


def test_a_lost_sandbox_without_a_kill_is_reported_lost(monkeypatch):
    registry = ProcessRegistry()

    def lose_the_backend():
        raise RuntimeError("sandbox reaped")

    env = _SandboxEnv(hooks={"check": lose_the_backend})
    session = _sandbox_session(registry, monkeypatch, env)

    _poll_sandbox(registry, session, env)

    assert (session.exit_code, session.completion_reason, session.termination_source) == (
        -1, "lost", "backend_lost")
    assert session.id in registry._finished
    assert session._completion_event.is_set()


def test_sandbox_output_fetched_before_a_kill_is_not_added_after_it(monkeypatch):
    """The kill published its snapshot; a delta the poller already had in hand must not grow
    the session's output past what the caller and the completion were given."""
    registry = ProcessRegistry()
    killed = []
    env = _SandboxEnv(delta="FINAL", alive=True,
                      hooks={"delta": lambda: killed.append(registry.kill_process("proc_sandbox"))})
    session = _sandbox_session(registry, monkeypatch, env)
    session.append_output("partial")

    _poll_sandbox(registry, session, env)

    assert killed[0]["output"] == "partial"
    assert session.output_buffer == "partial"


def test_a_log_rotation_seen_after_a_kill_does_not_clear_its_output(monkeypatch):
    registry = ProcessRegistry()
    env = _SandboxEnv(delta="partial", alive=True)
    session = _sandbox_session(registry, monkeypatch, env)
    execute = env.execute

    def rotated_on_the_second_read(command, **kwargs):
        if command.startswith("O=7;"):  # second log read: the log shrank, so the offset restarts
            registry.kill_process("proc_sandbox")
            return {"output": "3 0\nnew"}
        return execute(command, **kwargs)

    monkeypatch.setattr(env, "execute", rotated_on_the_second_read)
    _poll_sandbox(registry, session, env)

    assert session.output_buffer == "partial"


def _scoped_session(registry, monkeypatch, sid, stops, **fields) -> ProcessSession:
    """A session with its own systemd scope; ``stops`` lists each stop's outcome in turn."""
    session = _session(sid, systemd_unit=f"hermes-worker-{sid}.scope", **fields)
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_write_checkpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "_stop_systemd_unit", lambda _unit: stops.pop(0))
    return session


def test_a_recovered_pid_that_dies_mid_kill_reports_the_failed_scope_stop(monkeypatch):
    """``get`` sees the recovered PID alive, ``_signal_kill`` sees it gone: the scope stop it
    then runs can still fail, and that must not be reported as a clean exit."""
    registry = ProcessRegistry()
    session = _scoped_session(registry, monkeypatch, "proc_recovered", [False],
                              pid=4242, detached=True, pid_scope="host")
    fates = iter(["running", "gone"])
    monkeypatch.setattr(registry, "_detached_host_fate", lambda *_args: next(fates))

    result = registry.kill_process(session.id)

    assert result["status"] == "already_exited"
    assert result["scope_stop_failed"] == session.systemd_unit
    assert session.id in registry._finished  # keeps its unit for a retry


def test_bulk_cleanup_does_not_count_a_failed_scope_stop_and_retries_it(monkeypatch):
    """The root exits after ``kill_all`` picked the session, and the scope stop fails."""
    registry = ProcessRegistry()
    stops = [False, False, True]
    session = _scoped_session(registry, monkeypatch, "proc_bulk_scope", stops)
    kill_process = registry.kill_process

    def root_exits_first(sid, **kwargs):
        if not session.exited:
            registry._finish_exited(session, 0)
        return kill_process(sid, **kwargs)

    monkeypatch.setattr(registry, "kill_process", root_exits_first)

    assert registry.kill_all() == 0
    assert session.id in registry._finished
    assert registry.kill_all() == 0, "the scope still did not stop"
    assert stops == [True], "the finished session's scope stop was not retried"
    assert registry.kill_all() == 1
    assert registry.kill_all() == 0
    assert stops == [], "a stopped scope was retried"


def _failed_scope_stop(registry, monkeypatch, stops) -> ProcessSession:
    """A long-running session whose root just exited and whose scope stop then failed."""
    session = _scoped_session(registry, monkeypatch, "proc_pending_scope", stops)
    session.started_at -= module.FINISHED_TTL_SECONDS + 1
    registry._finish_exited(session, 0)
    assert "scope_stop_failed" in registry.kill_process(session.id)
    return session


def test_ttl_pruning_keeps_a_session_whose_scope_stop_is_pending(monkeypatch):
    registry = ProcessRegistry()
    stops = [False, True]
    session = _failed_scope_stop(registry, monkeypatch, stops)

    with registry._lock:
        registry._prune_if_needed()

    assert session.id in registry._finished
    assert registry.kill_all() == 1 and stops == []
    with registry._lock:
        registry._prune_if_needed()
    assert session.id not in registry._finished, "a stopped scope is pruned as usual"


def test_capacity_pruning_keeps_a_session_whose_scope_stop_is_pending(monkeypatch):
    registry = ProcessRegistry()
    session = _failed_scope_stop(registry, monkeypatch, [False])
    for i in range(module.MAX_PROCESSES):
        registry._finished[f"proc_newer_{i}"] = _session(f"proc_newer_{i}", exited=True)

    with registry._lock:
        registry._prune_if_needed()

    assert session.id in registry._finished
    assert "proc_newer_0" not in registry._finished  # the oldest prunable one goes instead
