"""Disposable subreaper and writer children for process-list lifecycle proof."""
import ctypes
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import threading

def _probe(root):
    import tools.process_registry as module
    from tests.waiting import wait_until

    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    module._SYSTEMD_SCOPE_AVAILABLE = False  # Own the test process tree, not a host service.
    registry = module.ProcessRegistry()
    sessions = []
    command = f"exec {shlex.quote(sys.executable)} {shlex.quote(__file__)}"
    try:
        for name in ("owner", "sibling"):
            session = registry.spawn_local(
                f"{command} child {shlex.quote(str(root / name))}",
                cwd=str(root), task_id=name + "-task", owner_task_id=name + "-owner",
                session_key=name + "-session",
            )
            sessions.append(session)
            session.notify_on_complete = True
        owner, sibling = sessions
        wait_until(lambda: all(name + "-output" in session.output_buffer
                               for name, session in zip(("owner", "sibling"), sessions)),
                   timeout=5, what="writers to become ready")
        assert all(s.process.poll() is None for s in sessions)
        (root / "owner-exit").touch()
        assert owner.process.wait(timeout=5) == 0
        assert owner._reader_thread.is_alive()  # Writer is still producing output.
        started = time.monotonic()
        listed = registry.list_sessions(session_key="owner-session")
        elapsed = time.monotonic() - started
        print(json.dumps({"direct_child": owner.process.returncode, "listed": listed,
                          "elapsed": elapsed, "reader_alive": owner._reader_thread.is_alive()}), flush=True)
        assert elapsed < 2, "list waited for the descendant's pipe lifetime"
        assert [entry["session_id"] for entry in listed] == [owner.id]
        assert listed[0]["status"] == "exited"
        assert listed[0]["exit_code"] == 0
        event = registry.completion_queue.get(timeout=2)
        assert (event["session_id"], event["session_key"], event["task_id"], event["owner_task_id"]) == (
            owner.id, "owner-session", "owner-task", "owner-owner")
        assert event["exit_code"] == 0 and "owner-output" in event["output"]
        assert registry.unread_completions_owned_by("owner-owner") == [owner]
        assert registry.unread_completions_owned_by("sibling-owner") == []
        for _ in range(3):
            assert registry.list_sessions(session_key="owner-session")[0]["status"] == "exited"
            foreign = registry.list_sessions(session_key="sibling-session")
            assert [(row["session_id"], row["status"]) for row in foreign] == [(sibling.id, "running")]
        assert sibling.process.poll() is None
        (root / "owner-stop").touch()
        owner._reader_thread.join(timeout=5)
        assert not owner._reader_thread.is_alive()
        assert registry.completion_queue.empty(), "reader and list emitted duplicate completions"
        assert not registry.is_completion_consumed(owner.id)
        assert "owner-output" in registry.read_log(owner.id)["output"]
        assert registry.is_completion_consumed(owner.id)
        print("PASS: list-only exit; one exact-owner event; running sibling isolated; unread output retained", flush=True)
    finally:
        for name in ("owner", "sibling"):
            (root / (name + "-stop")).touch()
            (root / (name + "-exit")).touch()
        for session in sessions:
            try:
                os.killpg(session.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            session.process.wait(timeout=5)
            session._reader_thread.join(timeout=5)
        # This subprocess contains only our two child trees.
        while True:
            try:
                os.waitpid(-1, 0)
            except ChildProcessError:
                break


def _child(gate):
    subprocess.Popen(
        [sys.executable, __file__, "writer", str(gate)], stdin=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while not Path(str(gate) + "-exit").exists() and time.monotonic() < deadline:
        threading.Event().wait(0.01)  # This scrubbed child cannot import the test helper package.


def _writer(gate):
    deadline = time.monotonic() + 15
    while not Path(str(gate) + "-stop").exists() and time.monotonic() < deadline:
        print(gate.name + "-output", flush=True)
        threading.Event().wait(0.02)  # Pace the real writer; this is not an assertion's readiness delay.


if __name__ == "__main__":
    {"probe": _probe, "child": _child, "writer": _writer}[sys.argv[1]](Path(sys.argv[2]))