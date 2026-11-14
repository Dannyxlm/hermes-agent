"""Review probe (PR #204 delta): the compute-host CHILD half of the vault pinning, as the child process
runs it: requests leave on the host pipe, answers come back as `respond` control frames. Synthetic data."""
import io
import json
import threading
from tests.waiting import wait_until

from tui_gateway import server as srv
from tui_gateway import server_requests as requests
from tui_gateway.compute_host import ComputeHost


def test_child_open_evaluate_close_round_trip_over_the_host_pipe(monkeypatch):
    requests.reset_for_tests()
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "probe-child"
    monkeypatch.setitem(srv._sessions, sid, {"transport": host._transport, "history_lock": threading.Lock()})

    def frames():
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def wait_request(operation):
        found = []
        def received():
            for frame in frames():
                message = frame.get("message") or {}
                if (frame.get("type") == "rpc" and message.get("method") == "preview.act"
                        and message["params"]["vault"]["operation"] == operation):
                    found.append(message)
                    return True
            return False
        assert wait_until(received, timeout=5), f"child never sent {operation}"
        return found[0]

    results, errors = {}, {}

    def ask(operation, **vault):
        try:
            results[operation] = requests.send(
                "preview.act", sid, {"action": "vault", "vault": {"operation": operation, **vault}}, timeout=4)
        except Exception as exc:
            errors[operation] = f"{type(exc).__name__}: {exc}"

    for operation, extra in (("open", {}), ("evaluate", {"target": "T", "expression": "synthetic-script"}),
                             ("close", {"target": "T"})):
        worker = threading.Thread(target=ask, args=(operation,), kwargs=extra)
        worker.start()
        if operation not in errors:
            try:
                message = wait_request(operation)
                host.handle_frame({"type": "respond", "sid": sid, "request_id": "r-" + operation, "params": {
                    "frame": {"id": message["id"], "result": {"value": json.dumps({"success": True, "target": "T"})}}}})
            except AssertionError as exc:
                errors.setdefault(operation, str(exc))
        worker.join(6)
    acks = {f["request_id"]: f.get("response", {}).get("result", {}).get("status") for f in frames()
            if f.get("type") == "respond.ack"}
    cancels = [(f.get("sid"), f["message"]["params"]["payload"].get("method"), f["message"]["params"]["payload"].get("reason"),
                "seq" in f["message"]["params"])
               for f in frames() if f.get("type") == "rpc" and (f.get("message") or {}).get("method") == "event"
               and f["message"]["params"].get("type") == "request.cancel"]
    print("PROBE-CHILD results=", {k: bool(v) for k, v in results.items()}, "errors=", errors, "acks=", acks,
          "cancels(sid,method,reason,has_seq)=", cancels)
    assert not errors
    assert all(results.get(op) for op in ("open", "evaluate", "close"))
    assert acks == {"r-open": "ok", "r-evaluate": "ok", "r-close": "ok"}
