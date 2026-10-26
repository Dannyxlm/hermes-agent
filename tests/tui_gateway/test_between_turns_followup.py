"""Child publication and responsive FIFO regressions for opt-in compaction."""
import copy
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost, _history_meta
from tui_gateway.host_supervisor import HostSupervisor
from tests.tui_gateway.test_between_turns_compaction import _fixture, _transcript


@pytest.mark.parametrize("in_place", [True, False])
def test_compute_host_child_compaction_updates_parent_between_turns(tmp_path, monkeypatch, in_place):
    """Real child transport -> supervisor -> parent relay, without another turn."""
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    db, agent, child, before = _fixture(tmp_path, monkeypatch, in_place=in_place)
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    child["transport"] = host._transport
    # The parent has only a metadata mirror; it must not construct a second agent.
    parent = {"agent": None, "history_lock": threading.Lock(), "history": copy.deepcopy(before),
              "session_key": child["session_key"], "history_version": 1,
              "running": False, "_compute_host_active": True}
    monkeypatch.setitem(server._sessions, "sid", parent)
    monkeypatch.setattr(server, "_transfer_active_session_slot", lambda *a, **kw: True)
    supervisor = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False,
                                rpc_sink=server._relay_compute_host_rpc)
    delivered = threading.Event()
    original_emit = host.emit

    def emit(frame):
        original_emit(frame)
        # Exercise JSON framing, not shared dict references between parent and child.
        supervisor._handle_host_frame(json.loads(json.dumps(frame)))
        if (frame.get("message") or {}).get("method") == "compute_host.session_meta":
            delivered.set()

    monkeypatch.setattr(host, "emit", emit)
    # _HostTransport captured the bound emit in __init__.
    host._transport._emit = emit
    stale_end = {"type": "turn.end", "sid": "sid", **_history_meta(child),
                 "session_info": {"model": "stale-model"}, "session_info_emitted": True}
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a, **kw: None)
    job = None
    try:
        assert "_compute_host_active" not in child
        job = server._start_between_turns_compaction("sid", child, {"completed": True})
        assert job is not None, "turn isolation must not disable between-turns compaction"
        assert job.done.wait(10)
        assert delivered.wait(2), "child committed but did not notify the parent"
        assert parent["agent"] is None
        assert parent["history_version"] == child["history_version"] == 2
        assert parent["session_key"] == child["session_key"] == agent.session_id
        assert (agent.session_id == "between-turns-parent") is in_place
        assert len(parent["history"]) < len(before)
        assert _transcript(parent["history"]) == _transcript(db.get_messages_as_conversation(agent.session_id))
        assert parent["_metadata_message_count"] == len(child["history"])
        assert parent["_metadata_mirror"]["model"] == agent.model
        # turn.end may have captured the old snapshot before the compaction, yet
        # reach the parent afterwards. Neither identity nor metadata may roll back.
        parent["running"] = True
        server._on_compute_host_turn_done("old-turn", "sid", parent, stale_end)
        assert parent["history_version"] == 2
        assert parent["session_key"] == agent.session_id
        assert parent["_metadata_message_count"] == len(child["history"])
        assert parent["_metadata_mirror"]["model"] == agent.model
        assert not parent["running"] and not child["running"]
    finally:
        if job is not None:
            job.done.wait(10)
        host.close()
        agent.close()
        db.close()


@pytest.mark.parametrize("method", ["session.interrupt", "session.steer", "prompt.submit"])
def test_opted_in_control_ignores_saturated_long_handler_pool(monkeypatch, method):
    """Adapted from the reviewer's saturated-pool probe, with the desired contract."""
    pool = ThreadPoolExecutor(max_workers=8)
    gate, ran = threading.Event(), threading.Event()
    started = [threading.Event() for _ in range(8)]
    session = {"agent": SimpleNamespace(compression_between_turns=True), "history_lock": threading.Lock()}
    monkeypatch.setitem(server._sessions, "sid", session)
    monkeypatch.setattr(server, "_pool", pool)

    def handle(req):
        if req["method"] == "slash.exec":
            started[req["id"]].set()
            assert gate.wait(10)
        else:
            ran.set()
        return {"id": req["id"], "result": {}}

    monkeypatch.setattr(server, "_handle_admitted_request", handle)
    transport = SimpleNamespace(write=lambda response: None)
    try:
        for rid in range(8):
            server.dispatch({"id": rid, "method": "slash.exec", "params": {}}, transport)
        assert all(event.wait(5) for event in started)
        assert session.get("_between_turns_compaction") is None
        server.dispatch({"id": "control", "method": method, "params": {"session_id": "sid"}}, transport)
        assert ran.wait(2), "control waited for unrelated long handlers"
        assert not gate.is_set()
    finally:
        gate.set()
        pool.shutdown(wait=True)
        tail = session.get("_between_turns_rpc_tail")
        if tail is not None:
            tail.result(timeout=5)


@pytest.mark.parametrize("deferred", [False, True])
def test_rejected_fifo_submit_replies_and_releases_retirement(monkeypatch, deferred):
    from concurrent.futures import Future
    from hermes_cli.backend_retirement import retirement

    def reject(fn):
        raise RuntimeError("executor unavailable")

    rejecting = SimpleNamespace(submit=reject)
    monkeypatch.setattr(server, "_pool", rejecting)
    # On base the shared pool is used; on the fix the dedicated pool is used.
    monkeypatch.setattr(server, "_compaction_rpc_pool", rejecting, raising=False)
    session = {"agent": SimpleNamespace(compression_between_turns=True)}
    monkeypatch.setitem(server._sessions, "sid", session)
    responses = []
    before = retirement.active_count()
    previous = None
    if deferred:
        previous = session["_between_turns_rpc_tail"] = Future()
    for rid in ("first", "second"):
        server.dispatch({"id": rid, "method": "session.interrupt", "params": {"session_id": "sid"}},
                        SimpleNamespace(write=responses.append))
    if previous is not None:
        assert responses == []
        assert retirement.active_count() == before + 2
        previous.set_result(None)
    assert [response["id"] for response in responses] == ["first", "second"]
    assert all(response["error"]["code"] == -32000 for response in responses)
    assert all("executor unavailable" in response["error"]["message"] for response in responses)
    assert retirement.active_count() == before
    assert session["_between_turns_rpc_tail"].done()
