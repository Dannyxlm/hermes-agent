"""Operation ownership survives queue loss, marker refusal and ambiguous child delivery."""

import io
import json
import time
import types
from pathlib import Path

import pytest

from tests.tui_gateway.test_attachment_operations import call, upload
from tests.tui_gateway.test_compute_host_borrowed_lease import _foreign_acquire, _seed_parent_lease
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

pytest_plugins = ["tests.tui_gateway.test_attachment_operation_admission"]


class Pipe:
    def __init__(self, failure):
        self.failure = failure
        self.frames = []

    def write(self, text):
        self.frames.append(json.loads(text))
        if self.failure == "write":
            raise OSError("write may have delivered bytes")

    def flush(self):
        if self.failure == "flush":
            raise OSError("flush may have delivered bytes")


@pytest.fixture
def isolated(session, admission, monkeypatch):
    supervisor = HostSupervisor(rpc_sink=lambda _: None, autostart=False)
    proc = types.SimpleNamespace(stdin=Pipe("flush"), poll=lambda: None, pid=123)
    supervisor._proc = proc
    monkeypatch.setattr(supervisor, "start", lambda: None)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_: supervisor)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: True)
    # Reconciliation is exercised explicitly; no timer or child is spawned by this fixture.
    monkeypatch.setattr("threading.Timer.start", lambda _: None)
    monkeypatch.setattr(server, "_TURN_SETTLE_BEFORE_CLOSE_SECONDS", 0)
    return supervisor, proc


@pytest.mark.parametrize("failure", ["write", "flush"])
@pytest.mark.parametrize("settlement", ["completion", "death"])
def test_ambiguous_pipe_delivery_keeps_real_lease_until_settlement(
    session, admission, isolated, monkeypatch, failure, settlement,
):
    supervisor, proc = isolated
    proc.stdin.failure = failure
    upload()
    lease = _seed_parent_lease("conversation", "live")
    session["active_session_lease"] = lease
    try:
        assert call("prompt.submit", text="inspect", attachment_operation_ids=["image-operation"])["error"]["code"] == 5019
        turn_id = session.get("_compute_host_turn_id")
        assert turn_id and turn_id in supervisor._pending_turns
        assert session["running"] and session["_compute_host_active"]
        assert call("prompt.submit", text="later", image_paths=[])["result"]["status"] == "queued"
        assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == "dispatching"
        assert server._teardown_popped_session(server._pop_session_by_id("live"))
        assert not lease.released
        foreign, refusal = _foreign_acquire("conversation")
        assert foreign is None and refusal.reason == "SESSION_NOT_OWNED"
        if settlement == "completion":
            supervisor._handle_host_frame({"type": "turn.end", "request_id": turn_id, "sid": "live"})
        else:
            proc.poll = lambda: 1
            supervisor._fail_pending_turns(reason="crash", message="confirmed child death")
        assert not session["running"] and not session.get("_compute_host_turn_id")
        assert lease.released and session.get("queued_prompt") is None
        session.update(_closing=False, _finalized=False)
        server._sessions["live"] = session
        assert call("prompt.submit", text="retry", attachment_operation_ids=["image-operation"])["error"]["code"] == 4020
        proc.stdin.failure = None
        assert call("prompt.submit", text="fresh", image_paths=[])["result"]["status"] == "streaming"
    finally:
        server._release_deferred_active_session_lease(session)
        lease.release()


@pytest.mark.parametrize("queued", [False, True])
def test_supervisor_start_refusal_clears_running_without_inline_replay(session, admission, isolated, monkeypatch, queued):
    supervisor, _proc = isolated
    staged = upload()["result"]

    def refused():
        raise OSError("host cannot start")

    monkeypatch.setattr(supervisor, "start", refused)
    session["running"] = queued
    response = call("prompt.submit", text="inspect", attachment_operation_ids=["image-operation"])
    if queued:
        assert response["result"]["status"] == "queued"
        session["running"] = False
        assert server._drain_queued_prompt("drain", "live", session)
    else:
        assert response["error"]["code"] == 5019
    assert not session["running"] and not session.get("_compute_host_turn_id")
    assert supervisor._pending_turns == {} and admission == []
    assert Path(staged["path"]).exists()
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == "staged"


def test_uncertain_queued_dispatch_remains_fenced_after_queue_disappears(
    session, admission, isolated, monkeypatch,
):
    supervisor, _proc = isolated
    upload()
    session["running"] = True
    assert call("prompt.submit", text="queued inspect", attachment_operation_ids=["image-operation"])["result"]["status"] == "queued"
    session["running"] = False
    assert server._drain_queued_prompt("drain", "live", session)
    assert session["running"] and session["_compute_host_turn_id"] in supervisor._pending_turns
    monkeypatch.setattr("tui_gateway.turn_marker.marker_writer_state", lambda _: "dead")
    server._sessions["live"] = {**session, "running": False, "queued_prompt": None}
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == "submitted"
    assert call("attachment.operation.detach", operation_id="image-operation")["error"]["code"] == 4020
    assert call("prompt.submit", text="retry", attachment_operation_ids=["image-operation"])["error"]["code"] == 4020


def test_stale_uncertain_child_lease_requires_confirmed_death(session, admission, isolated, monkeypatch):
    supervisor, proc = isolated
    upload()
    lease = _seed_parent_lease("conversation", "live")
    session["active_session_lease"] = lease
    monkeypatch.setattr(supervisor, "_terminate_process", lambda _: None)
    try:
        assert call("prompt.submit", text="inspect", attachment_operation_ids=["image-operation"]).get("error")
        server._settle_isolated_turn_before_close(session)
        server._deferred_active_session_lease_ages[lease.lease_id] = time.time() - server._DEFERRED_ACTIVE_SESSION_LEASE_TTL_SECONDS - 1
        assert server._reap_stale_deferred_leases() == 0
        assert not lease.released and session.get("_compute_host_turn_id")
        proc.poll = lambda: 1
        assert server._reap_stale_deferred_leases() == 1
        assert lease.released and not session.get("_compute_host_turn_id")
    finally:
        server._release_deferred_active_session_lease(session)
        lease.release()


def test_marker_replacement_refusal_prevents_operation_invocation_and_restart_replay(
    session, admission, invocation, monkeypatch,
):
    import tui_gateway.turn_marker as markers
    upload()
    home = Path(session["profile_home"])
    record_turn_start(home, "conversation", "inspect", auto_continue=True)
    real_store = markers._store

    def refuse_suppression(path, entries):
        if entries.get("conversation", {}).get("auto_continue") is False:
            raise OSError("only marker directory is unwritable")
        real_store(path, entries)

    monkeypatch.setattr(markers, "_store", refuse_suppression)
    assert call("prompt.submit", text="inspect", attachment_operation_ids=["image-operation"])["result"]["status"] == "streaming"
    admission[0].run()
    assert invocation == []
    assert not session["running"]
    assert read_turn_marker(home, "conversation") is None
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == "submitted"
    monkeypatch.setattr(server, "marker_writer_state", lambda _: "dead")
    assert server._maybe_schedule_auto_continue("live", {**session, "running": False}, "conversation") is None


@pytest.mark.parametrize("loss", ["stop", "restart"])
def test_confirmed_predispatch_queue_loss_restores_staged_operation(
    session, admission, monkeypatch, loss,
):
    staged = upload()["result"]
    session["running"] = True
    assert call("prompt.submit", text="queued inspect", attachment_operation_ids=["image-operation"])["result"]["status"] == "queued"
    if loss == "stop":
        server._interrupt_session_turn("live", session)
    else:
        server._sessions["live"] = {**session, "running": False, "queued_prompt": None}
        server._sessions["live"].pop("queued_prompts", None)
        # Model a genuinely dead queue writer, without pretending to be on another OS.
        monkeypatch.setattr("tui_gateway.turn_marker.marker_writer_state", lambda _: "dead")
    status = call("attachment.operation.status", operation_id="image-operation")["result"]
    assert status["state"] == "staged"
    assert Path(staged["path"]).read_bytes() == b"image bytes"
    assert call("attachment.operation.detach", operation_id="image-operation")["result"]["state"] == "detached"


@pytest.mark.parametrize("race", ["completion", "stop"])
def test_queue_cannot_dispatch_until_acceptance_releases_receipt_locks(
    session, admission, invocation, monkeypatch, race,
):
    upload()
    session["running"] = True

    def raced_accept(*_):
        if race == "stop":
            server._interrupt_session_turn("live", session)
        else:
            session["running"] = False
            assert server._drain_queued_prompt("drain", "live", session) is False

    monkeypatch.setattr(server, "_persist_queued_user_row", raced_accept)
    assert call("prompt.submit", text="queued inspect", attachment_operation_ids=["image-operation"])["result"]["status"] == "queued"
    assert len(invocation) == (1 if race == "completion" else 0)
    assert session.get("queued_prompt") is None
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == ("submitted" if race == "completion" else "staged")


@pytest.mark.parametrize("route", ["inline", "queued-inline", "compute", "queued-compute"])
def test_explicit_empty_selection_reaches_agent_and_preserves_desktop_queue(
    session, admission, invocation, monkeypatch, route,
):
    desktop = Path(session["profile_home"]) / "desktop.png"
    desktop.write_bytes(b"desktop original")
    session["attached_images"] = [str(desktop)]
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    frames = []

    def submit(frame, **_):
        frames.append(frame)
        parent = server._sessions["live"]
        try:
            server._sessions["live"] = {**parent, "running": False, "attached_images": frame["attached_images"]}
            host._run_real_turn(frame)
        finally:
            server._sessions["live"] = parent

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_: types.SimpleNamespace(submit_turn=submit))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: "compute" in route)
    session["running"] = route.startswith("queued")
    try:
        assert call("prompt.submit", text="text only", image_paths=[])["result"]["status"] == ("queued" if route.startswith("queued") else "streaming")
        if route.startswith("queued"):
            session["running"] = False
            assert server._drain_queued_prompt("drain", "live", session)
        elif route == "inline":
            admission[0].run()
    finally:
        host.close()
    assert len(invocation) == 1 and str(desktop) not in invocation[0][0]
    assert session["attached_images"] == [str(desktop)]
    assert desktop.read_bytes() == b"desktop original"
    if frames:
        assert frames[0]["attached_images"] == []


def test_reattached_same_path_survives_deferred_explicit_admission(session, admission, invocation):
    image = Path(session["profile_home"]) / "same.png"
    image.write_bytes(b"same original")
    assert call("image.attach", path=str(image))["result"]["attached"]
    assert call("prompt.submit", text="first", image_paths=[str(image)])["result"]["status"] == "streaming"
    assert call("image.attach", path=str(image))["result"]["attached"]
    admission[0].run()
    assert "same.png" in invocation[0][0]
    assert session["attached_images"] == [str(image)]


@pytest.mark.parametrize("route", ["queued-inline", "compute", "queued-compute"])
def test_same_path_reattached_during_reserved_handoff_survives_busy_and_compute(
    session, admission, invocation, monkeypatch, route,
):
    image = Path(session["profile_home"]) / "same.png"
    image.write_bytes(b"same original")
    assert call("image.attach", path=str(image))["result"]["attached"]
    original_rebind = server._rebind_submit_transport

    def reattach(*args):
        assert call("image.attach", path=str(image))["result"]["attached"]
        return original_rebind(*args)

    monkeypatch.setattr(server, "_rebind_submit_transport", reattach)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)

    def submit(frame, **_):
        parent = server._sessions["live"]
        try:
            server._sessions["live"] = {**parent, "running": False, "attached_images": frame["attached_images"]}
            host._run_real_turn(frame)
        finally:
            server._sessions["live"] = parent

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_: types.SimpleNamespace(submit_turn=submit))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: "compute" in route)
    session["running"] = route.startswith("queued")
    try:
        assert call("prompt.submit", text="inspect", image_paths=[str(image)]).get("result")
        if route.startswith("queued"):
            session["running"] = False
            assert server._drain_queued_prompt("drain", "live", session)
    finally:
        host.close()
    assert len(invocation) == 1 and str(image) in invocation[0][0]
    assert session["attached_images"] == [str(image)]
