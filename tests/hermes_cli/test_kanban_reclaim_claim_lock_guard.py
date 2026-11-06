"""Tests: reclaim paths are claim-lock-aware so they can't desync a re-claimed
task (issue #36910).

A stale crash/stale-claim/max-runtime reclaim, computed from a snapshot of an
OLD worker, used to reset ``tasks.status`` back to ``ready`` with only a
``WHERE status='running'`` guard. If the task had since been reclaimed AND
re-claimed by a NEW worker (new run, new claim_lock, live pid), that stale
UPDATE clobbered the live task: ``tasks.status='ready'`` while the new
``task_runs.status='running'`` and the worker kept executing — the board showed
the task in the Ready lane and the dispatcher could treat live work as
available. The reset is now gated on the snapshot's ``claim_lock`` (and pid),
so it only fires when the task is still owned by the worker the reclaim was
computed for.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def test_stale_crash_reset_rejected_for_reclaimed_task(conn):
    """A reset carrying an OLD worker's claim_lock must NOT clobber a task
    that has since been re-claimed by a new worker."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="desync", assignee="w")

    # Worker A claims, then dies.
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kbd._set_worker_pid(conn, tid, dead.pid)
    old = conn.execute(
        "SELECT claim_lock, worker_pid FROM tasks WHERE id=?", (tid,)
    ).fetchone()

    # Reclaim + re-claim by worker B (alive).
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, current_run_id=NULL WHERE id=?",
        (tid,),
    )
    conn.commit()
    kb.claim_task(conn, tid, claimer=f"{host}:B")
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        kbd._set_worker_pid(conn, tid, sleeper.pid)

        # The stale reset for worker A — same shape as the guarded UPDATE in
        # detect_crashed_workers — must reject (rowcount 0) because B owns it.
        cur = conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL "
            "WHERE id=? AND status='running' AND worker_pid=? AND claim_lock IS ?",
            (tid, old["worker_pid"], old["claim_lock"]),
        )
        conn.commit()
        assert cur.rowcount == 0, "stale reclaim wrongly clobbered the re-claimed task"

        final = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert final["status"] == "running"
        assert final["claim_lock"] == f"{host}:B"
    finally:
        sleeper.terminate()


def test_genuine_crash_still_reclaims(conn):
    """When the claim_lock still matches the dead worker, the crash reclaim
    fires normally — the guard must not break the legitimate path."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="legit", assignee="w")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kbd._set_worker_pid(conn, tid, dead.pid)
    # Rewind started_at so the launch grace window doesn't skip the check.
    conn.execute("UPDATE tasks SET started_at = started_at - 9999 WHERE id=?", (tid,))
    conn.execute(
        "UPDATE task_runs SET started_at = started_at - 9999 WHERE task_id=?", (tid,)
    )
    conn.commit()
    kbd._record_worker_exit(dead.pid, 1 << 8)  # nonzero exit → crash

    crashed = kbd.detect_crashed_workers(conn)
    assert tid in crashed
    final = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert final["status"] in ("ready", "blocked", "todo")


def _sweep(conn, path):
    if path == "pid":
        return kbd.detect_crashed_workers(conn)
    return kb.release_stale_claims(conn, signal_fn=_unexpected_signal, failure_limit=2)


def _unexpected_signal(*args):
    pytest.fail(f"fixture reclaim attempted to signal a process: {args}")


def _prepare_attempt(conn, tid, path, claim):
    task = claim(conn, tid)
    assert task is not None
    assert task.current_run_id is not None
    if path == "pid":
        # Prove this PID absent rather than touching a running host process.
        pid = 3_800_000
        while psutil.pid_exists(pid):
            pid += 1
        kbd._set_worker_pid(conn, tid, pid)
        kbd._record_worker_exit(pid, 1 << 8)
    else:
        conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tid,))
        conn.commit()
    return task.current_run_id


@pytest.mark.parametrize("path", ["pid", "ttl"])
@pytest.mark.parametrize("phase", ["ready", "review"])
def test_reclaim_commit_cannot_lose_or_duplicate_failure_accounting(conn, monkeypatch, path, phase):
    """Death immediately after the reclaim COMMIT must leave the entire failure durable.

    A fresh connection/sweep must see one booking, then allow a real new claim below
    budget. Losing the second reclaim's caller must still trip the breaker exactly once.
    """
    tid = kb.create_task(conn, title="recoverable attempt", assignee="worker", max_retries=2)
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (phase, tid))
    conn.commit()
    claim = kb.claim_review_task if phase == "review" else kb.claim_task
    real_boundary = kbc._execute_boundary_with_retry
    outcome = "crashed" if path == "pid" else "reclaimed"
    path_to_db = kb.kanban_db_path()

    for expected_failures in (1, 2):
        run_id = _prepare_attempt(conn, tid, path, claim)
        committed = []

        def die_after_reclaim_commit(store, sql):
            real_boundary(store, sql)
            run = kb.get_run(store, run_id)
            assert run is not None
            if sql == "COMMIT" and run.outcome == outcome:
                committed.append(run_id)
                raise RuntimeError("dispatcher died after reclaim commit")

        with monkeypatch.context() as fault:
            fault.setattr(kbc, "_execute_boundary_with_retry", die_after_reclaim_commit)
            with pytest.raises(RuntimeError, match="dispatcher died after reclaim commit"):
                _sweep(conn, path)
        assert committed == [run_id]

        with contextlib.closing(kbc.connect(path_to_db)) as recovered:
            task = kb.get_task(recovered, tid)
            assert task is not None
            expected_status = phase if expected_failures == 1 else "blocked"
            assert task.status == expected_status
            assert task.consecutive_failures == expected_failures
            assert task.claim_lock is task.worker_pid is task.current_run_id is None
            run = kb.get_run(recovered, run_id)
            assert run is not None
            assert run.outcome == outcome and run.ended_at is not None
            before = kb.list_events(recovered, tid)
            assert not _sweep(recovered, path)
            assert kb.get_task(recovered, tid) == task
            assert kb.list_events(recovered, tid) == before
            gave_up = [event for event in before if event.kind == "gave_up"]
            assert len(gave_up) == expected_failures - 1
            if expected_failures == 1:
                assert kbd.check_respawn_guard(recovered, tid, lane=phase) is None
            else:
                assert gave_up[0].payload is not None
                assert gave_up[0].payload["failures"] == 2
                assert gave_up[0].payload["effective_limit"] == 2
                assert gave_up[0].payload["trigger_outcome"] == outcome
                assert claim(recovered, tid) is None


@pytest.mark.parametrize("path", ["pid", "ttl"])
def test_reclaim_accounting_failure_rolls_back_run_release_and_breaker(conn, monkeypatch, path):
    tid = kb.create_task(conn, title="atomic reclaim", assignee="worker", max_retries=1)
    run_id = _prepare_attempt(conn, tid, path, kb.claim_task)
    task_before, run_before = kb.get_task(conn, tid), kb.get_run(conn, run_id)
    events_before = kb.list_events(conn, tid)
    real_append = kb._append_event

    def fail_after_breaker_event(store, task_id, kind, *args, **kwargs):
        real_append(store, task_id, kind, *args, **kwargs)
        if kind == "gave_up":
            raise RuntimeError("accounting write interrupted")

    with monkeypatch.context() as fault:
        fault.setattr(kb, "_append_event", fail_after_breaker_event)
        with pytest.raises(RuntimeError, match="accounting write interrupted"):
            _sweep(conn, path)
    assert kb.get_task(conn, tid) == task_before
    assert kb.get_run(conn, run_id) == run_before
    assert kb.list_events(conn, tid) == events_before
    assert _sweep(conn, path)
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "blocked"
    assert task.consecutive_failures == 1
    assert not _sweep(conn, path)


@pytest.mark.parametrize("path", ["pid", "ttl"])
def test_reclaim_entrypoint_does_not_account_against_a_replacement_claim(conn, monkeypatch, path):
    from hermes_cli import kanban_owner_recovery

    tid = kb.create_task(conn, title="replacement claim", assignee="worker", max_retries=1)
    old_run = _prepare_attempt(conn, tid, path, kb.claim_task)
    replacements = []

    def replace_after_snapshot(store, task_id, **kwargs):
        # Interleave replacement after the sweep's SELECT, before its claim CAS.
        # This fixture writes on the sweep connection because the PID sweep already
        # holds SQLite's writer lock; no other writer can interleave at that point.
        if replacements:
            return False
        lock = kb._host_prefix() + "replacement"
        run_id = store.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, started_at) "
            "VALUES (?, 'worker', 'running', ?, 1)", (task_id, lock),
        ).lastrowid
        store.execute(
            "UPDATE tasks SET current_run_id = ?, claim_lock = ?, claim_expires = 4000000000, "
            "worker_pid = NULL, worker_started_at = NULL WHERE id = ?", (run_id, lock, task_id),
        )
        replacements.append((kb.get_task(store, task_id), kb.get_run(store, run_id)))
        return False

    monkeypatch.setattr(kanban_owner_recovery, "owner_reclaim_blocked", replace_after_snapshot)
    assert not _sweep(conn, path)
    assert len(replacements) == 1
    task, run = replacements[0]
    assert task is not None and run is not None
    assert kb.get_task(conn, tid) == task
    assert kb.get_run(conn, run.id) == run
    old = kb.get_run(conn, old_run)
    assert old is not None and old.outcome is None
    assert task.status == "running" and task.consecutive_failures == 0
    assert not [e for e in kb.list_events(conn, tid) if e.kind in ("crashed", "reclaimed", "gave_up")]


@pytest.mark.parametrize("path", ["pid", "ttl"])
def test_reclaim_observer_reads_committed_run_release_and_breaker(conn, monkeypatch, path):
    from hermes_cli.plugins import get_plugin_manager

    tid = kb.create_task(conn, title="committed observer", assignee="worker", max_retries=1)
    run_id = _prepare_attempt(conn, tid, path, kb.claim_task)
    hook = "on_kanban_worker_exited" if path == "pid" else "on_kanban_worker_stale_claim"
    observed = []

    def read_committed(**kwargs):
        with contextlib.closing(kbc.connect(kb.kanban_db_path())) as reader:
            observed.append((conn.in_transaction, kwargs["run_id"],
                             kb.get_task(reader, tid), kb.get_run(reader, run_id),
                             [e.kind for e in kb.list_events(reader, tid)]))

    monkeypatch.setitem(get_plugin_manager()._hooks, hook, [read_committed])
    assert _sweep(conn, path)
    assert len(observed) == 1
    in_transaction, observed_run_id, task, run, kinds = observed[0]
    assert in_transaction is False and observed_run_id == run_id
    assert task is not None and run is not None
    assert task.status == "blocked" and task.consecutive_failures == 1
    assert task.claim_lock is task.worker_pid is task.current_run_id is None
    assert run.outcome == ("crashed" if path == "pid" else "reclaimed")
    assert kinds[-1] == "gave_up"
