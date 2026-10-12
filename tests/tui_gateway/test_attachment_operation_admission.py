"""Operation admission owns media once and leaves crash recovery to its receipt."""

import errno
import io
import json
import os
import stat
import types
from pathlib import Path

import pytest

from tests.tui_gateway.test_attachment_operations import call, upload
from tui_gateway import server
from tui_gateway.attachment_operations import AttachmentOperations
from tui_gateway.compute_host import ComputeHost
from tui_gateway.turn_marker import read_turn_marker, record_turn_start


pytest_plugins = ["tests.tui_gateway.test_attachment_operations"]

_PERSIST_SUBMIT = server._persist_session_row_for_submit


class DeferredThread:
    def __init__(self, target, **_):
        self.target = target

    def start(self):
        pass

    def run(self):
        self.target()


@pytest.fixture
def admission(session, monkeypatch):
    threads = []

    def defer(**kwargs):
        thread = DeferredThread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr("agent.memory_provider.spawn_context_thread", defer)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_: None)
    monkeypatch.setattr(server, "_reattach_refusal", lambda *_: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: False)
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *_: None)
    monkeypatch.setattr(server, "_persist_queued_user_row", lambda *_: None)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(server, "_load_cfg", dict)
    session.update(agent=types.SimpleNamespace(), history_version=0, cols=80)
    return threads


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("fault", ["open", "fsync"])
def test_dispatch_directory_sync_failure_never_starts_or_queues(
    session, admission, monkeypatch, busy, fault,
):
    staged = upload()["result"]
    receipt_path = Path(staged["path"]).parent / "receipt.json"
    session["running"] = busy
    real_open, real_fsync = os.open, os.fsync

    def fail_open(path, flags, *args, **kwargs):
        if (fault == "open" and Path(path).is_dir()
                and json.loads(receipt_path.read_text(encoding="utf-8"))["state"] == "dispatching"):
            raise OSError("directory open refused")
        return real_open(path, flags, *args, **kwargs)

    def fail_fsync(fd):
        if (fault == "fsync" and stat.S_ISDIR(os.fstat(fd).st_mode)
                and json.loads(receipt_path.read_text(encoding="utf-8"))["state"] == "dispatching"):
            raise OSError("directory sync refused after rename")
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", fail_open)
    monkeypatch.setattr(os, "fsync", fail_fsync)
    response = call("prompt.submit", text="inspect", attachment_operation_ids=["image-operation"])
    assert response["error"]["code"] == 5027
    assert admission == []
    assert session.get("queued_prompt") is None


@pytest.mark.platforms("posix")
def test_new_operation_ancestor_sync_failure_refuses_tombstone(session, monkeypatch):
    store = server._attachment_operation_store(session, {"session_id": "live"})
    real_open, real_fsync = os.open, os.fsync
    tracked = {}

    def track_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        tracked[fd] = Path(path)
        return fd

    def fail_parent_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and tracked.get(fd) == store.root.parent:
            raise OSError("new ancestor entry not durable")
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fsync", fail_parent_sync)
    assert call("attachment.operation.detach", operation_id="missing")["error"]["code"] == 5027
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert call("attachment.operation.detach", operation_id="missing")["result"]["state"] == "detached"


def test_explicit_raw_image_is_reserved_before_deferred_build(session, admission, monkeypatch):
    session["attached_images"] = ["selected.png", "desktop.png"]
    first = call("prompt.submit", text="first", image_paths=["selected.png"])
    assert first["result"]["status"] == "streaming"
    second = call("prompt.submit", text="legacy second", queued=True)
    assert second["result"]["status"] == "queued"
    assert session["queued_prompt"]["image_paths"] == ["desktop.png"]
    assert session["attached_images"] == []
    received = []
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: received.append(k["image_paths"]))
    admission[0].run()
    assert received == [["selected.png"]]


@pytest.mark.parametrize("code", [5070, 5071, 5072])
@pytest.mark.parametrize("busy_race", [False, True])
def test_storage_refusal_keeps_operation_selectable_and_detachable(
    session, admission, monkeypatch, code, busy_race,
):
    staged = upload()["result"]
    session["attached_images"] = ["selected.png", "desktop.png"]
    monkeypatch.setattr(server, "_persist_session_row_for_submit", _PERSIST_SUBMIT)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_: code != 5072)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_: None)
    monkeypatch.setattr(server, "_db_error", "temporary store unavailable")

    def refuse_user_row(*_):
        raise OSError(errno.ENOSPC if code == 5070 else errno.EACCES, "injected storage failure")

    monkeypatch.setattr(server, "_persist_submit_user_row", refuse_user_row)
    if busy_race:
        session["running"] = True

        def became_idle(*args, **kwargs):
            session["running"] = False

        monkeypatch.setattr(server, "_handle_busy_submit", became_idle)
    response = call("prompt.submit", text="inspect", image_paths=["selected.png"],
                    attachment_operation_ids=["image-operation"])
    assert response["error"]["code"] == code
    assert session["running"] is False
    assert session.get("inflight_turn") is None
    assert admission == []
    assert session["attached_images"] == ["selected.png", "desktop.png"]
    store = server._attachment_operation_store(session, {"session_id": "live"})
    with store.selection(["image-operation"]) as receipts:
        assert receipts[0]["image_paths"] == [staged["path"]]
    assert call("attachment.operation.detach", operation_id="image-operation")["result"]["state"] == "detached"
    assert not Path(staged["path"]).exists()


@pytest.fixture
def invocation(session, admission, monkeypatch):
    calls = []

    def run(message, **kwargs):
        calls.append((message, kwargs, read_turn_marker(Path(session["profile_home"]), "conversation")))
        return {"final_response": "done"}

    session["agent"] = types.SimpleNamespace(
        session_id="conversation", api_mode="codex_app_server", run_conversation=run,
        clear_interrupt=lambda: None,
    )
    for name in ("_wire_callbacks", "_sync_agent_model_with_config", "_register_session_cwd",
                 "_sync_session_key_after_compress", "_post_turn_housekeeping", "_run_post_turn_followups"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_: None)
    monkeypatch.setattr(server, "_get_usage", lambda *_: {})
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *_: (
        types.SimpleNamespace(set=lambda: None), types.SimpleNamespace(join=lambda: None)))

    def inline(target, **_):
        target()
        return types.SimpleNamespace(is_alive=lambda: False)

    monkeypatch.setattr(server, "_start_session_work", inline)
    return calls


@pytest.mark.parametrize("route", ["inline", "queued-inline", "compute", "queued-compute"])
def test_idle_and_drained_operation_media_handoff_disables_restart_replay(
    session, admission, invocation, monkeypatch, route,
):
    first, second = upload("first")["result"], upload("second", b"second original")["result"]
    session["attached_images"] = ["desktop.png"]
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
        response = call("prompt.submit", text="inspect", image_paths=[], attachment_operation_ids=["second", "first"])
        assert response["result"]["status"] == ("queued" if route.startswith("queued") else "streaming")
        if route.startswith("queued"):
            session["running"] = False
            assert server._drain_queued_prompt("drain", "live", session)
        elif route == "inline":
            admission[0].run()
    finally:
        host.close()
    assert len(invocation) == 1
    message, _kwargs, marker = invocation[0]
    assert second["path"] in message and first["path"] in message
    assert message.index(second["path"]) < message.index(first["path"])
    assert "desktop.png" not in message
    assert session["attached_images"] == ["desktop.png"]
    assert Path(second["path"]).read_bytes() == b"second original"
    assert marker["auto_continue"] is False
    if frames:
        assert frames[0]["auto_continue"] is False
    # A fresh dead-writer marker reproduces the restart input. Recovery must schedule nothing.
    record_turn_start(Path(session["profile_home"]), "conversation", marker["prompt"], auto_continue=marker["auto_continue"])
    monkeypatch.setattr(server, "marker_writer_state", lambda *_: "dead")
    restarted = {**session, "running": False}
    before = len(admission)
    assert server._maybe_schedule_auto_continue("live", restarted, "conversation") is None
    assert len(admission) == before
    assert call("prompt.submit", text="retry", attachment_operation_ids=["first"])["error"]["code"] == 4020


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("saved", [False, True])
def test_accepted_receipt_write_failure_fences_retry_after_one_accept(
    session, admission, invocation, monkeypatch, busy, saved,
):
    upload()
    selected = Path(session["profile_home"]) / "selected.png"
    selected.write_bytes(b"selected original")
    session.update(running=busy, attached_images=[str(selected), "desktop.png"])

    accepted = AttachmentOperations.accepted

    def refuse_receipt(store, receipts, result):
        if saved:
            accepted(store, receipts, result)
        raise OSError("accepted receipt disk full")

    monkeypatch.setattr(AttachmentOperations, "accepted", refuse_receipt)
    response = call("prompt.submit", text="inspect", image_paths=[str(selected)], attachment_operation_ids=["image-operation"])
    assert response["error"]["code"] == 5027
    assert len(admission) == (0 if busy else 1)
    assert bool(session.get("queued_prompt")) is busy
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == ("submitted" if saved else "dispatching")
    assert call("prompt.submit", text="retry", attachment_operation_ids=["image-operation"])["error"]["code"] == 4020
    assert session["attached_images"] == ["desktop.png"]
    if busy:
        session["running"] = False
        assert server._drain_queued_prompt("drain", "live", session)
    else:
        admission[0].run()
    assert len(invocation) == 1
    assert "selected.png" in invocation[0][0]
    assert invocation[0][2]["auto_continue"] is False


def test_uncertain_compute_dispatch_cannot_fall_back_or_restore_media(session, admission, monkeypatch):
    upload()
    session["attached_images"] = ["selected.png", "desktop.png"]
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: True)

    def uncertain(frame, **_):
        raise OSError("pipe flush failed after frame write")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_: types.SimpleNamespace(submit_turn=uncertain))
    response = call("prompt.submit", text="inspect", image_paths=["selected.png"], attachment_operation_ids=["image-operation"])
    assert response["error"]["code"] == 5019
    assert admission == []
    assert session["attached_images"] == ["desktop.png"]
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == "dispatching"
    assert call("prompt.submit", text="retry", attachment_operation_ids=["image-operation"])["error"]["code"] == 4020


def test_legacy_idle_turn_keeps_automatic_crash_recovery(session, admission, invocation):
    assert call("prompt.submit", text="legacy")["result"]["status"] == "streaming"
    admission[0].run()
    assert len(invocation) == 1
    assert invocation[0][2]["auto_continue"] is True
