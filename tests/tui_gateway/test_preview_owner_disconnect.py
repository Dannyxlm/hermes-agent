"""Review probe (PR #204 delta): the window that answered a vault `open` disconnects before the
child's follow-up frame reaches the parent. Synthetic data only."""
import io
import json
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server as srv
from tui_gateway import server_requests as requests
from tui_gateway.host_supervisor import HostSupervisor
from tui_gateway.transport import bind_transport, reset_transport


def _vault(sid, rid, operation, **vault):
    return {"jsonrpc": "2.0", "id": rid, "method": "preview.act", "params": {
        "session_id": sid, "action": "vault", "vault": {"operation": operation, **vault}}}


@pytest.mark.parametrize("followup", ["close", "evaluate"])
def test_owner_disconnect_before_followup_does_not_kill_the_host_reader(monkeypatch, tmp_path, followup):
    sid = "probe-owner-gone"
    session = {"history_lock": threading.Lock()}
    monkeypatch.setitem(srv._sessions, sid, session)
    requests.reset_for_tests()
    broadcast, owner_frames = [], []
    monkeypatch.setattr(srv, "write_json", lambda frame: broadcast.append(frame) or True)
    monkeypatch.setattr(srv, "_session_uses_compute_host", lambda session: True)
    monkeypatch.setattr(srv, "_get_compute_host_supervisor", lambda: SimpleNamespace(
        respond=lambda sid, payload, **kw: {"type": "respond.ack", "response": {"result": {"status": "ok"}}}))
    class _Window:
        def write(self, frame):
            owner_frames.append(frame)
            return True

    owner = _Window()
    sup = HostSupervisor(rpc_sink=srv._relay_compute_host_rpc, registry_path=tmp_path / "registry.json",
                         autostart=False)
    def rpc(message):
        return json.dumps({"type": "rpc", "sid": sid, "message": message}) + "\n"

    # 1. The isolated child opens a preview page; the Desktop window showing the chat answers.
    sup._handle_host_frame(json.loads(rpc(_vault(sid, "o1", "open"))))
    token = bind_transport(owner)
    try:
        assert srv._relay_compute_host_response(
            {"id": "o1", "result": {"value": json.dumps({"success": True, "target": "T"})}})
    finally:
        reset_transport(token)
    # 2. That window's socket drops (ws.py::_teardown_transport does exactly this).
    srv.unregister_live_transport(owner)
    # 3. The child's next frame for the pinned page arrives, followed by an ordinary event of any session.
    vault = {"target": "T", **({"expression": "synthetic-secret-script"} if followup == "evaluate" else {})}
    later = {"jsonrpc": "2.0", "method": "event", "params": {
        "type": "message.delta", "session_id": sid, "payload": {"text": "still alive"}}}
    orphan = _vault(sid, "f1", followup, **vault)
    assert srv._relay_compute_host_rpc(orphan) is False
    proc = SimpleNamespace(stdout=io.StringIO(rpc(orphan) + rpc(later)))
    sup._proc = proc
    died = []

    def drain():
        try:
            sup._drain_stdout(proc)
        except BaseException as exc:  # the real thread has no handler: this is its death
            died.append(f"{type(exc).__name__}: {exc}")

    reader = threading.Thread(target=drain)
    reader.start()
    reader.join(10)
    print("PROBE", followup, "reader_died=", died, "later_delivered=", later in broadcast,
          "followup_broadcast=", any(f.get("id") == "f1" for f in broadcast),
          "followup_to_owner=", any(f.get("id") == "f1" for f in owner_frames))
    assert not any(f.get("id") == "f1" for f in broadcast), "secret-bearing follow-up was broadcast"
    assert not died, f"compute-host stdout reader died: {died}"
    assert later in broadcast, "frames after the vault follow-up were never relayed"


def test_raising_sink_does_not_discard_later_host_frames(tmp_path):
    delivered = []

    def sink(message):
        if message.get("id") == "bad":
            raise ValueError("synthetic sink failure")
        delivered.append(message)

    sup = HostSupervisor(rpc_sink=sink, registry_path=tmp_path / "registry.json", autostart=False)
    later = {"method": "event", "params": {"type": "message.delta"}}
    proc = SimpleNamespace(stdout=io.StringIO("".join(json.dumps({"type": "rpc", "message": m}) + "\n"
                                                    for m in ({"id": "bad"}, later))))
    sup._proc = proc
    sup._drain_stdout(proc)
    assert delivered == [later]


def test_raising_sink_does_not_skip_crashed_turns_or_respawn(tmp_path, monkeypatch):
    delivered, completed, respawned = [], [], []

    def sink(message):
        delivered.append(message["params"]["session_id"])
        raise ValueError("synthetic sink failure")

    sup = HostSupervisor(rpc_sink=sink, registry_path=tmp_path / "registry.json", autostart=False)
    proc = SimpleNamespace(wait=lambda: 1)
    sup._proc = proc
    sup._pending_turns = {"r1": ("s1", completed.append), "r2": ("s2", completed.append)}
    monkeypatch.setattr(sup, "_remove_registry", lambda: None)
    monkeypatch.setattr(sup, "_maybe_respawn_after_crash", lambda: respawned.append(True))
    sup._wait_for_exit(proc)
    assert delivered == ["s1", "s2"]
    assert [frame["request_id"] for frame in completed] == ["r1", "r2"]
    assert respawned == [True]
