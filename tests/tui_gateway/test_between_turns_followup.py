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
@pytest.mark.parametrize("respawned", [False, True])
def test_compute_host_child_compaction_updates_parent_between_turns(tmp_path, monkeypatch, in_place, respawned):
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
    if respawned:
        # A previous incarnation left a high parent watermark. The replacement
        # starts a fresh counter, but its rotations and history are authoritative.
        server._apply_compute_host_metadata_mirror(parent, {
            "sid": "sid", "boot_id": "previous-boot", "history_version": 7,
            "session_key": parent["session_key"]})
    if respawned:
        # Exercise the normal _init_session rebuild, not its exception fallback.
        monkeypatch.setattr(server, "_make_agent", lambda *a, **kw: agent)
        for name in ("_wire_session_agent", "_start_session_services", "_schedule_mcp_late_refresh",
                     "_hydrate_session_cwd", "_register_session_cwd"):
            monkeypatch.setattr(server, name, lambda *a, **kw: None)
        monkeypatch.setitem(server._sessions, "sid", child)
        child = host._build_server_session(server, {
            "sid": "sid", "session_key": parent["session_key"], "history": before,
            "history_version": 7, "cwd": str(tmp_path)}, "sid")
        assert child["history_version"] == 0
    monkeypatch.setitem(server._sessions, "sid", child)
    host._ensure_server_session(server, {"sid": "sid"})
    initial_child_version = child["history_version"]
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
        assert child["history_version"] == initial_child_version + 1
        assert parent["history_version"] == (7 if respawned else 2)
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
        assert parent["history_version"] == (7 if respawned else 2)
        assert parent["session_key"] == agent.session_id
        assert parent["_metadata_message_count"] == len(child["history"])
        assert parent["_metadata_mirror"]["model"] == agent.model
        assert not parent["running"] and not child["running"]
        # The replacement's next turn.end still carries a smaller counter than
        # the parent's max. It must update metadata without reverting rotation.
        child["history_version"] += 1
        parent["running"] = True
        server._on_compute_host_turn_done("next-turn", "sid", parent, {
            "type": "turn.end", "sid": "sid", **_history_meta(child),
            "session_info": {"model": "next-model"}, "session_info_emitted": True})
        assert parent["session_key"] == agent.session_id
        assert parent["_metadata_mirror"]["model"] == "next-model"
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


@pytest.mark.parametrize("previous_child_frame", [False, True])
@pytest.mark.parametrize("route", ["turn.end", "compress", "slash", "late.compress"])
def test_default_off_respawn_adopts_rotated_key_despite_parent_version(monkeypatch, route, previous_child_frame):
    """Reviewer respawn probes: OFF must retain base key/lease/mirror behavior."""
    monkeypatch.setattr(server, "_load_cfg", lambda: {"compression": {"between_turns": False}})
    parent = {"agent": None, "history_lock": threading.Lock(), "history": [],
              "session_key": "A", "history_version": 7, "running": True,
              "_compute_host_active": True}
    transfers = []
    monkeypatch.setitem(server._sessions, "sid", parent)
    monkeypatch.setattr(server, "_transfer_active_session_slot",
                        lambda sid, session, new_session_id: transfers.append(new_session_id) or True)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_status_update", lambda *a, **kw: None)
    if previous_child_frame:
        # Ordinary/late controls must keep base's unconditional adoption when no
        # between-turns publication exists, even after another child snapshot.
        server._apply_compute_host_metadata_mirror(parent, {
            "sid": "sid", "session_key": "A", "history_version": 9})
    frame = {"type": "turn.end", "sid": "sid", "session_key": "B", "history_version": 2,
             "message_count": 4, "session_info": {"model": "child-model"}, "session_info_emitted": True}
    if route == "turn.end":
        server._on_compute_host_turn_done("r", "sid", parent, frame)
        assert parent["running"] is False
    elif route == "late.compress":
        server._adopt_late_compute_host_compress_ack("sid", parent, frame, route_name="session.compress")
    else:
        # Synchronous compress and slash acks share this exact adoption helper.
        server._apply_compute_host_metadata_mirror(parent, frame)
    assert parent["session_key"] == "B", "fresh child rotation was dropped"
    assert transfers == ["B"]
    assert parent["history_version"] == (9 if previous_child_frame else 7)
    assert parent["_metadata_message_count"] == 4
    assert parent["_metadata_mirror"]["model"] == "child-model"


def test_new_child_boot_accepts_reset_counter_and_rejects_retired_boot(monkeypatch):
    parent = {"history_lock": threading.Lock(), "session_key": "A", "history_version": 50}
    monkeypatch.setattr(server, "_transfer_active_session_slot", lambda *a, **kw: True)
    def adopt(boot, version, key):
        server._apply_compute_host_metadata_mirror(parent, {
            "sid": "sid", "boot_id": boot, "history_version": version, "session_key": key,
            "history": [{"role": "user", "content": key}], "message_count": version,
            "session_info": {"model": key}})
    adopt("old", 7, "A")
    adopt("replacement", 1, "B")
    assert parent["session_key"] == "B"
    # Even an old boot's larger counter is not authority after replacement.
    adopt("old", 100, "A")
    assert parent["session_key"] == "B"
    assert parent["history"] == [{"role": "user", "content": "B"}]
    assert parent["_metadata_mirror"]["model"] == "B"
    assert parent["history_version"] == 50


def test_supervisor_drops_frames_from_replaced_process(tmp_path):
    relayed = []
    supervisor = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False,
                                rpc_sink=relayed.append)
    old = SimpleNamespace(stdout=io.StringIO(json.dumps({
        "type": "rpc", "message": {"method": "compute_host.session_meta",
        "params": {"sid": "sid", "session_key": "old"}}}) + "\n"))
    supervisor._proc = SimpleNamespace()
    supervisor._drain_stdout(old)
    assert relayed == [], "retired stdout drain rolled parent metadata back"


def test_child_commit_interrupt_keeps_reader_live_and_orders_next_turn(tmp_path, monkeypatch):
    from tests.tui_gateway.test_between_turns_review_regressions import _block_commit
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    db, agent, child, _ = _fixture(tmp_path, monkeypatch)
    entered, release = _block_commit(db, monkeypatch)
    monkeypatch.setitem(server._sessions, "sid", child)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    child["transport"] = host._transport
    reader_returned, next_turn, interrupted = threading.Event(), threading.Event(), threading.Event()
    replies = []
    def emit(frame):
        replies.append(frame)
        if frame["type"] == "interrupt.ack":
            interrupted.set()
    host.emit = emit
    # Keep actual preemption but leave unrelated interrupt side effects out.
    monkeypatch.setattr(server, "_interrupt_session_turn",
                        lambda sid, session: server._preempt_between_turns_compaction(session))
    monkeypatch.setattr(host, "_run_real_turn", lambda frame: next_turn.set())
    def read():
        host.handle_frame({"type": "interrupt", "sid": "sid", "request_id": "stop"})
        host.handle_frame({"type": "turn.start", "sid": "sid", "request_id": "next"})
        host.handle_frame({"type": "respond", "sid": "missing", "request_id": "other"})
        reader_returned.set()
    job = server._start_between_turns_compaction("sid", child, {"completed": True})
    reader = threading.Thread(target=read)
    try:
        assert entered.wait(5)
        reader.start()
        assert reader_returned.wait(2), "child stdin reader blocked behind commit"
        assert any(f.get("request_id") == "other" for f in replies)
        assert not interrupted.is_set() and not next_turn.is_set()
        release.set()
        assert interrupted.wait(5) and next_turn.wait(5)
        assert replies[0]["request_id"] == "other"
    finally:
        release.set()
        reader.join(10)
        job.done.wait(10)
        host.close()
        agent.close()
        db.close()


@pytest.mark.parametrize("failure", ["serialization", "encoding"])
def test_child_metadata_write_failure_still_restarts_worker_and_emits_info(tmp_path, monkeypatch, caplog, failure):
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    db, agent, child, _ = _fixture(tmp_path, monkeypatch, in_place=False)
    actions, finished = [], threading.Event()
    def write(frame):
        if failure == "serialization":
            raise TypeError("cannot serialize metadata")
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")
    child["transport"] = SimpleNamespace(write=write)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *a: actions.append("restart"))
    def info(*a):
        actions.append("info")
        finished.set()
    monkeypatch.setattr(server, "_emit_session_info", info)
    job = server._start_between_turns_compaction("sid", child, {"completed": True})
    try:
        assert job is not None and job.done.wait(10)
        assert finished.wait(2), "metadata write skipped local post-publication work"
        assert actions == ["restart", "info"]
        assert child["session_key"] == agent.session_id != "between-turns-parent"
        assert "compute-host compaction metadata publication failed" in caplog.text
    finally:
        job.done.wait(10)
        agent.close()
        db.close()


def test_late_ack_from_unseen_old_boot_cannot_replace_current_child(monkeypatch):
    """A control ack can be queued before respawn but adopted after it."""
    monkeypatch.setattr(server, "_compute_host_supervisor", SimpleNamespace(boot_id="current"))
    monkeypatch.setattr(server, "_transfer_active_session_slot", lambda *a, **kw: True)
    parent = {"history_lock": threading.Lock(), "session_key": "B", "history_version": 8}
    server._apply_compute_host_metadata_mirror(parent, {
        "sid": "sid", "boot_id": "current", "history_version": 1, "session_key": "B"})
    server._apply_compute_host_metadata_mirror(parent, {
        "sid": "sid", "boot_id": "unseen-old", "history_version": 100, "session_key": "A"})
    assert parent["session_key"] == "B"
    assert parent["history_version"] == 8


_DYING_CHILD = r'''
import json, os, sys
N = int(sys.argv[1])
def out(f):
    sys.stdout.write(json.dumps(f) + "\n"); sys.stdout.flush()
out({"type": "hello", "host_pid": os.getpid(), "boot_id": "dying-boot", "build_sha": "", "hermes_home": ""})
for raw in sys.stdin:
    f = json.loads(raw)
    if f.get("type") == "control":
        for i in range(N):
            out({"type": "rpc", "sid": "sid", "message": {"jsonrpc": "2.0", "method": "event",
                 "params": {"type": "message.delta", "session_id": "sid", "payload": {"i": i}}}})
        out({"type": "control.ack", "sid": "sid", "request_id": f["request_id"],
             "route_name": "session.compress", "session_key": "B", "history_version": 9})
        os._exit(3)
'''


@pytest.mark.parametrize("n,sink_delay", [(0, 0.0), (300, 0.002)])
def test_frames_written_before_child_death_are_still_delivered(tmp_path, n, sink_delay):
    """Crash path, no replacement: base delivered every frame the dead child had already
    written (including a /compress ack carrying the rotated key). Only a LIVE replacement
    may fence a retired reader; a nulled ``_proc`` is not a replacement."""
    import queue
    import sys
    import time

    script = tmp_path / "dying_child.py"
    script.write_text(_DYING_CHILD)
    relayed = []

    def sink(msg):
        if sink_delay:
            time.sleep(sink_delay)
        relayed.append(msg)

    supervisor = HostSupervisor(registry_path=tmp_path / "h.json", argv=[sys.executable, str(script), str(n)],
                                cwd=tmp_path, env={}, rpc_sink=sink, respawn_max=0,
                                expected_build_sha="unknown", expected_hermes_home="", autostart=False)
    supervisor.start()
    ack = None
    with __import__("contextlib").suppress(queue.Empty):
        ack = supervisor.control("sid", route_name="session.compress", wait=True, timeout=8.0)
    deadline = time.monotonic() + 5.0
    while len(relayed) < n and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ack is not None and ack.get("session_key") == "B", "control.ack written before death was dropped"
    assert len(relayed) == n, f"only {len(relayed)}/{n} pre-death frames delivered"


def test_bootless_version_frame_is_legacy_and_does_not_retire_live_boot(monkeypatch):
    monkeypatch.setattr(server, "_transfer_active_session_slot", lambda *a, **kw: True)
    monkeypatch.setattr(server, "_compute_host_supervisor", SimpleNamespace(boot_id="b1"), raising=False)
    parent = {"history_lock": threading.Lock(), "session_key": "A", "history_version": 5}
    server._apply_compute_host_metadata_mirror(parent, {
        "sid": "sid", "boot_id": "b1", "session_key": "A", "history_version": 1})
    server._apply_compute_host_metadata_mirror(parent, {"sid": "sid", "session_key": "A", "history_version": 2})
    server._apply_compute_host_metadata_mirror(parent, {
        "sid": "sid", "boot_id": "b1", "session_key": "B", "history_version": 3})
    assert parent["session_key"] == "B", "a boot-less frame retired the live child boot"
    assert "b1" not in parent.get("_compute_host_retired_metadata_boots", set())
