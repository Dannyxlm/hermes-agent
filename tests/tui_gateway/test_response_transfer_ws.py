"""Exercise large recovery through actual ASGI WebSocket framing and dispatch."""

import base64
import json

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from tests.tui_gateway.test_methods_mobile import mobile_home
from tui_gateway import event_replay, response_transfers, server, server_requests
from tui_gateway.ws import handle_ws


def test_large_recovery_over_websocket_preserves_cut_and_question(mobile_home, monkeypatch):
    # Process watchers are unrelated to the wire contract and must not outlive the
    # disposable profile fixture. Keep real transport, dispatch, scope and spools.
    for name in ("_ensure_skin_watcher", "_ensure_lease_watcher",
                 "_start_backend_heartbeat_refresher", "_schedule_startup_orphan_sweep"):
        monkeypatch.setattr(server, name, lambda: None)
    event_replay.reset_replay_state()
    app = FastAPI()

    @app.websocket("/api/ws")
    async def socket(ws: WebSocket):
        await handle_ws(ws)

    received_sizes = []

    def call(ws, method, **params):
        ws.send_json({"jsonrpc": "2.0", "id": method, "method": method, "params": params})
        while True:
            wire = ws.receive_text()
            received_sizes.append(len(wire.encode("utf-8")))
            assert received_sizes[-1] < 4 * 1024 * 1024
            reply = json.loads(wire)
            if reply.get("id") == method:
                return reply

    with TestClient(app) as client, client.websocket_connect("/api/ws") as ws:
        assert json.loads(ws.receive_text())["params"]["type"] == "gateway.ready"
        capabilities = call(ws, "mobile.capabilities")["result"]
        assert "chunked_recovery" in capabilities["features"]
        assert capabilities["feature_versions"]["chunked_recovery"] == 1
        sid = call(ws, "session.resume", session_id="root", profile="default",
                   defer_history=True, omit_messages=True)["result"]["session_id"]
        segment = "é🙂" * 11000
        server._emit("message.start", sid)
        for _ in range(80):
            server._emit("message.interim", sid, {"text": segment, "already_streamed": False})
        question = server_requests.ServerRequest(sid, "clarify", {"question": segment * 80})
        with server_requests._lock:
            server_requests._open[question.id] = question
        reply = call(ws, "session.stream.snapshot", session_id=sid, chunked_response=True)
        descriptor = reply["result"]["response_transfer"]
        assert descriptor["byte_length"] > 4 * 1024 * 1024

        with client.websocket_connect("/api/ws") as foreign:
            foreign.receive_text()
            denied = call(foreign, "session.response.read", session_id=sid,
                          transfer_id=descriptor["transfer_id"], offset=0)
            assert denied["error"]["code"] == 4400

        def hydrate(descriptor):
            data = bytearray()
            while len(data) < descriptor["byte_length"]:
                chunk = call(ws, "session.response.read", session_id=sid,
                             transfer_id=descriptor["transfer_id"], offset=len(data))["result"]
                assert chunk["offset"] == len(data)
                data.extend(base64.b64decode(chunk["data"], validate=True))
                assert chunk["next_offset"] == len(data)
                assert chunk["eof"] == (len(data) == descriptor["byte_length"])
            released = call(ws, "session.response.release", session_id=sid,
                            transfer_id=descriptor["transfer_id"])
            assert released["result"]["released"] is True
            assert descriptor["transfer_id"] not in response_transfers.transfers._entries
            return json.loads(data)

        restored = hydrate(descriptor)
        assert "".join(restored["stream"]["segments"]) == segment * 80
        replay = call(ws, "session.events.since", session_id=sid,
                      last_seen=restored["baseline_seq"], chunked_response=True)
        replay_descriptor = replay["result"]["response_transfer"]
        assert replay_descriptor["byte_length"] > 4 * 1024 * 1024
        restored_replay = hydrate(replay_descriptor)
        assert restored_replay["open_requests"][0]["params"]["question"] == segment * 80
        assert restored_replay["events"] == []
        assert call(ws, "gateway.ping")["result"]["ok"] is True
    assert received_sizes and max(received_sizes) < 4 * 1024 * 1024
