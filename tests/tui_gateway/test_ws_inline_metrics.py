"""Inline mobile refusals must reach the same aggregate as pooled replies."""
import json

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from hermes_cli.route_metrics import RouteMetrics, RouteMetricsMiddleware
from tui_gateway import server, ws


def test_inline_mobile_scope_refusal_is_counted(monkeypatch, caplog):
    metrics = RouteMetrics()
    app = FastAPI()
    app.add_middleware(RouteMetricsMiddleware, metrics=metrics)
    method = "mobile.widget.register"
    monkeypatch.setitem(server._methods, method, lambda rid, params: server._err(rid, 4400, "invalid scope"))
    assert method not in server._LONG_HANDLERS
    for name in ("_ensure_skin_watcher", "_ensure_lease_watcher", "_start_backend_heartbeat_refresher",
                 "_schedule_startup_orphan_sweep"):
        monkeypatch.setattr(server, name, lambda: None)
    monkeypatch.setattr(ws, "_note_dashboard_client_activity", lambda **kw: None)

    @app.websocket("/api/ws")
    async def endpoint(socket: WebSocket):
        await ws.handle_ws(socket)

    with TestClient(app) as client:
        with client.websocket_connect("/api/ws", headers={"User-Agent": "HermesMobile/123"}) as socket:
            assert socket.receive_json()["method"] == "event"
            socket.send_json({"jsonrpc": "2.0", "id": "PRIVATE_ID_SENTINEL", "method": method,
                              "params": {"profile": "default", "stored_session_id": "fixture",
                                         "installation_id": "fixture-installation", "connection_id": "fixture-connection",
                                         "environment": "production", "read_token": "12" * 32}})
            reply = socket.receive_text()
            assert json.loads(reply)["error"]["code"] == 4400
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        metrics.emit_summary()
    logs = [r.message for r in caplog.records if r.name == "hermes_cli.web_server.route_metrics"]
    assert logs, "the inline 4400 refusal disappeared from release-check metrics"
    rows = json.loads(logs[-1].split("route_metrics ", 1)[1])
    assert len(rows) == 1
    assert rows[0]["label"] == ["rpc", method, "phone", "123", "error"]
    assert rows[0]["count"] == 1
    assert rows[0]["bytes_total"] == len(reply.encode("utf-8"))
    assert "PRIVATE_ID_SENTINEL" not in logs[-1]
