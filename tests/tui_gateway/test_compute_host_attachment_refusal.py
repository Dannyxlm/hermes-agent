"""An isolated child's correlated pre-start refusal releases only its queued operations."""

import io
import json
import threading
import types
from pathlib import Path

import pytest

from tests.tui_gateway.test_attachment_operation_reconciliation import Pipe
from tests.tui_gateway.test_attachment_operations import call, upload
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor

pytest_plugins = ["tests.tui_gateway.test_attachment_operation_admission"]

_USES_COMPUTE_HOST = server._session_uses_compute_host


class SplitComputeProcesses:
    def __init__(self, parent_records, monkeypatch):
        self.parent_records = parent_records
        self.child_records = {}
        self.output = io.StringIO()
        self.host = ComputeHost(stdout=self.output, heartbeat_secs=0)
        self.pipe = Pipe(None)
        self.supervisor = HostSupervisor(rpc_sink=lambda _: None, autostart=False)
        self.starts = []
        self.received = []

        def start():
            self.starts.append(True)
            if self.supervisor._proc is None:
                self.supervisor._proc = types.SimpleNamespace(stdin=self.pipe, poll=lambda: None, pid=123)

        monkeypatch.setattr(self.supervisor, "start", start)
        monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_: self.supervisor)
        monkeypatch.setattr("threading.Timer.start", lambda _: None)

    def child_frame(self, frame):
        offset = len(self.output.getvalue())
        server._sessions = self.child_records
        try:
            if frame["type"] == "turn.start":
                self.host._run_real_turn(frame)
            else:
                self.host.handle_frame(frame)
        finally:
            server._sessions = self.parent_records
        replies = [json.loads(line) for line in self.output.getvalue()[offset:].splitlines()]
        self.received.extend(replies)
        # Dispatch the actual child's frames only after restoring the parent process map.
        for reply in replies:
            self.supervisor._handle_host_frame(reply)
        return replies

    def close(self):
        self.host.close()


@pytest.fixture
def split_processes(session, admission, invocation, monkeypatch):
    child_agent = session["agent"]
    session.update(agent=None, agent_ready=threading.Event())
    monkeypatch.setattr(server, "_session_uses_compute_host", _USES_COMPUTE_HOST)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {"turn_isolation": True})
    monkeypatch.setattr(server, "_make_agent", lambda *a, **k: child_agent)
    for name in ("_hydrate_session_cwd", "_wire_session_agent", "_start_session_services", "_schedule_mcp_late_refresh"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    processes = SplitComputeProcesses(server._sessions, monkeypatch)
    try:
        yield processes, child_agent
    finally:
        processes.close()


@pytest.mark.parametrize("outcome", ["refused", "started-interrupted", "uncertain-write"])
def test_lazy_stop_then_child_queue_admission_reconciles_only_proven_refusal(
    session, admission, invocation, split_processes, outcome,
):
    processes, child_agent = split_processes
    staged = upload()["result"]
    assert processes.supervisor._proc is None
    assert call("session.interrupt")["result"]["status"] == "interrupted"
    assert session["_queued_prompt_generation"] == 1
    assert processes.starts == [] and processes.child_records == {} and processes.supervisor._proc is None

    assert call("prompt.submit", text="first ordinary turn", image_paths=[])["result"]["status"] == "streaming"
    ordinary = processes.pipe.frames[0]
    assert ordinary["queued_prompt_generation"] is None
    assert call("prompt.submit", text="inspect operation", attachment_operation_ids=["image-operation"])["result"]["status"] == "queued"
    if outcome == "uncertain-write":
        processes.pipe.failure = "flush"
    processes.child_frame(ordinary)

    child = processes.child_records["live"]
    assert child is not session and child["history_lock"] is not session["history_lock"]
    assert child.get("_queued_prompt_generation", 0) == 0
    assert session["_queued_prompt_generation"] == 1
    assert len(invocation) == 1
    operation = processes.pipe.frames[-1]
    assert operation["queued_prompt_generation"] == 1
    assert session["_compute_host_turn_id"] == operation["request_id"]
    assert operation["request_id"] in processes.supervisor._pending_turns

    if outcome == "uncertain-write":
        assert session["running"]
    else:
        if outcome == "started-interrupted":
            # An actual child-side Stop advances its own generation to match the
            # already-dispatched envelope; interrupt again inside model invocation.
            processes.child_frame({"type": "interrupt", "sid": "live", "request_id": "child-idle-stop"})
            assert child["_queued_prompt_generation"] == 1
            run = child_agent.run_conversation

            def interrupted_run(message, **kwargs):
                processes.host.handle_frame({"type": "interrupt", "sid": "live", "request_id": "child-live-stop"})
                return run(message, **kwargs)

            child_agent.run_conversation = interrupted_run
        replies = processes.child_frame(operation)
        end = next(reply for reply in replies if reply["type"] == "turn.end")
        assert end["request_id"] == operation["request_id"] and end["interrupted"] is True
        assert not session["running"] and not session.get("_compute_host_turn_id")
        started = [reply for reply in replies if reply["type"] == "turn.started"]
        assert len(started) == (0 if outcome == "refused" else 1)
        assert len(invocation) == (1 if outcome == "refused" else 2)

    assert Path(staged["path"]).read_bytes() == b"image bytes"
    status = call("attachment.operation.status", operation_id="image-operation")["result"]
    if outcome == "refused":
        assert status["state"] == "staged"
        store = server._attachment_operation_store(session, {"session_id": "live"})
        with store.selection(["image-operation"]) as receipts:
            assert receipts[0]["image_paths"] == [staged["path"]]
        assert call("attachment.operation.detach", operation_id="image-operation")["result"]["state"] == "detached"
        assert not Path(staged["path"]).exists()
    else:
        assert status["state"] == "submitted"
        assert call("attachment.operation.detach", operation_id="image-operation")["error"]["code"] == 4020
        assert call("prompt.submit", text="retry", attachment_operation_ids=["image-operation"])["error"]["code"] == 4020


def test_old_queue_refusal_cannot_restore_a_newer_acceptance(session, admission):
    upload()
    session["running"] = True
    assert call("prompt.submit", text="old queue", attachment_operation_ids=["image-operation"])["result"]["status"] == "queued"
    old = session["queued_prompt"]
    server._interrupt_session_turn("live", session)
    session["running"] = True
    assert call("prompt.submit", text="new queue", attachment_operation_ids=["image-operation"])["result"]["status"] == "queued"
    current = session["queued_prompt"]
    assert current["_attachment_queue"]["id"] != old["_attachment_queue"]["id"]
    server._restore_refused_queued_attachments(old, "live")
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == "submitted"
    assert session["queued_prompt"] is current
