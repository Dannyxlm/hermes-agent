"""List refresh observes the child, not its descendants' capture-pipe lifetime."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest


@pytest.mark.platforms("posix")
def test_list_leaves_live_reader_as_completion_owner():
    from tools.process_registry import ProcessRegistry, ProcessSession

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_owned_reader",
        command="owned-reader",
        task_id="owner-task",
        owner_task_id="owner-owner",
        session_key="owner-session",
        started_at=time.time(),
        notify_on_complete=True,
    )
    session.process = cast(Any, SimpleNamespace(
        poll=lambda: 0, stdout=None, stderr=None, stdin=None,
    ))
    session._reader_selectable = True
    session._reader_thread = cast(Any, SimpleNamespace(is_alive=lambda: True))
    registry._running[session.id] = session

    listed = registry.list_sessions(session_key="owner-session")

    assert listed[0]["status"] == "exited"
    assert session._reader_finish_requested.is_set()
    assert session.id in registry._running
    assert registry.completion_queue.empty()

    session.append_output("owner-output")
    registry._move_to_finished(session)
    event = registry.completion_queue.get_nowait()
    assert (event["owner_task_id"], event["output"]) == (
        "owner-owner", "owner-output",
    )


@pytest.mark.platforms("posix")
def test_poll_during_the_readers_final_drain_keeps_the_whole_output(tmp_path):
    """A child that ends on a burst exits with its tail still in the pipe. A poll landing while the reader
    is behind must report, and leave in the completion, the whole output — not the chunk read so far."""
    import threading

    import tools.process_registry as module

    module._SYSTEMD_SCOPE_AVAILABLE = False
    registry = module.ProcessRegistry()
    stalled, release = threading.Event(), threading.Event()

    def slow_sink(_session, _chunk):  # a live-output consumer that falls behind for a moment
        if not stalled.is_set():
            stalled.set()
            release.wait(10)

    registry.on_output = slow_sink
    burst = "import sys; sys.stdout.write('x' * 99 + '\\n'); sys.stdout.write(('y' * 99 + '\\n') * 100 + 'FINAL\\n')"
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} -c {shlex.quote(burst)}", cwd=str(tmp_path))
    session.notify_on_complete = True
    try:
        assert stalled.wait(10)
        assert session.process.wait(timeout=10) == 0
        threading.Timer(0.3, release.set).start()
        polled = registry.poll(session.id)
        assert session._completion_event.wait(10)
        assert polled["status"] == "exited"
        assert polled["output_preview"].rstrip().endswith("FINAL"), polled["output_preview"][-80:]
        event = registry.completion_queue.get(timeout=2)
        assert event["output"].rstrip().endswith("FINAL"), event["output"][-80:]
    finally:
        release.set()
        registry.kill_all(source="test")


@pytest.mark.platforms("linux")
def test_list_reconciles_real_exit_without_consuming_owned_result(tmp_path):
    # A disposable subreaper owns even the orphaned writer; no global pytest
    # process state is changed, and every fixture child is reaped on failure.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("fixtures") / "process_list_exit_probe.py"), "probe", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
