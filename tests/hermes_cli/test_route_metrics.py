"""Content-free aggregate metrics through the ASGI transport boundary."""
import json
import re
from types import SimpleNamespace

import pytest


def test_http_uses_route_template_and_counts_streamed_bytes(caplog):
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient
    from hermes_cli.route_metrics import RouteMetrics, RouteMetricsMiddleware

    metrics = RouteMetrics()
    app = FastAPI()
    app.add_middleware(RouteMetricsMiddleware, metrics=metrics)

    @app.get("/api/sessions/{id}/messages")
    async def messages(id: str):
        return StreamingResponse(iter([b"abc", b"defgh"]))

    with TestClient(app) as client:
        response = client.get("/api/sessions/abc123/messages?q=secret",
                              headers={"User-Agent": "HermesMobile/123"})
    assert response.content == b"abcdefgh"
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        metrics.emit_summary()
    row = json.loads(caplog.records[-1].message.split("route_metrics ", 1)[1])[0]
    assert row["label"] == ["http", "/api/sessions/{id}/messages", "HermesMobile", "123", "2xx"]
    assert row["count"] == 1
    assert row["bytes_total"] == 8
    assert row["p50_ms"] >= 0
    assert row["p95_ms"] >= row["p50_ms"]
    assert "abc123" not in caplog.records[-1].message
    assert "secret" not in caplog.records[-1].message


@pytest.mark.parametrize("pooled,known", [(True, True), (False, True), (False, False)])
def test_rpc_counts_completed_response_without_retaining_params(monkeypatch, caplog, pooled, known):
    from fastapi import FastAPI, WebSocket
    from fastapi.testclient import TestClient
    from hermes_cli.route_metrics import RouteMetrics, RouteMetricsMiddleware
    from tui_gateway import server, ws

    metrics = RouteMetrics()
    app = FastAPI()
    app.add_middleware(RouteMetricsMiddleware, metrics=metrics)
    from hermes_cli import route_metrics
    sentinel = "SENTINEL_private_title_token_é🙂"
    method = "metrics.test" if known else sentinel
    clock = [100.0]
    monkeypatch.setattr(route_metrics, "time", SimpleNamespace(perf_counter=lambda: clock[0]))

    def handler(rid, params):
        clock[0] += .09  # Includes pooled execution, not just dispatch/enqueue time.
        server.current_transport().write({"jsonrpc": "2.0", "method": "event",
                           "params": {"type": "test", "payload": {"text": sentinel}}})
        return server._ok(rid, {"text": params["text"]})

    if known:
        monkeypatch.setitem(server._methods, method, handler)
    if pooled:
        monkeypatch.setattr(server, "_LONG_HANDLERS", server._LONG_HANDLERS | {method})
    for name in ("_ensure_skin_watcher", "_ensure_lease_watcher", "_start_backend_heartbeat_refresher",
                 "_schedule_startup_orphan_sweep"):
        monkeypatch.setattr(server, name, lambda: None)
    monkeypatch.setattr(ws, "_note_dashboard_client_activity", lambda **kw: None)

    @app.websocket("/api/ws")
    async def endpoint(socket: WebSocket):
        await ws.handle_ws(socket)

    with TestClient(app) as client:
        agent = "HermesMobile/123" if known else "HermesMobile/PRIVATE_UA_SENTINEL"
        with client.websocket_connect("/api/ws", headers={"User-Agent": agent}) as socket:
            assert socket.receive_json()["method"] == "event"
            socket.send_json({"jsonrpc": "2.0", "id": sentinel, "method": method,
                              "params": {"text": sentinel}})
            if known:
                assert socket.receive_json()["method"] == "event"
            reply = socket.receive_text()
            if known:
                assert json.loads(reply)["result"]["text"] == sentinel
            else:
                assert json.loads(reply)["error"]["code"] == -32601
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        metrics.emit_summary()
    logs = [r.message for r in caplog.records if r.name == "hermes_cli.web_server.route_metrics"]
    assert logs, "the completed RPC must produce an aggregate"
    row = json.loads(logs[-1].split("route_metrics ", 1)[1])[0]
    expected = ["rpc", "metrics.test", "HermesMobile", "123", "success"] if known else [
        "rpc", "unknown_method", "unknown", "unknown", "error"]
    assert row["label"] == expected
    assert row["count"] == 1
    assert row["bytes_total"] == len(reply.encode("utf-8"))
    assert row["p95_ms"] == (100 if known else 1)
    assert not re.search(r"SENTINEL|Bearer|sk-|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", logs[-1])


def test_server_registers_outer_metrics_for_host_rejections(caplog, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_cli import route_metrics, web_server

    route_metrics.METRICS.emit_summary()
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    response = TestClient(web_server.app).get("/PRIVATE_PATH_SENTINEL?q=PRIVATE_QUERY_SENTINEL",
                                             headers={"Host": "evil.example",
                                                      "User-Agent": "PRIVATE_UA_SENTINEL"})
    assert response.status_code == 400
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        route_metrics.METRICS.emit_summary()
    logs = [r.message for r in caplog.records if r.name == "hermes_cli.web_server.route_metrics"]
    assert logs, "the server must instrument middleware rejections"
    row = json.loads(logs[-1].split("route_metrics ", 1)[1])[0]
    assert row["label"] == ["http", "rejected", "unknown", "unknown", "4xx"]
    assert row["bytes_total"] == len(response.content)
    assert "PRIVATE_" not in logs[-1]


def test_summary_is_emitted_on_interval_without_request_logging(monkeypatch, caplog):
    import time
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import route_metrics

    monkeypatch.setattr(route_metrics, "_SUMMARY_INTERVAL_S", .02, raising=False)
    metrics = route_metrics.RouteMetrics()
    app = FastAPI()
    app.add_middleware(route_metrics.RouteMetricsMiddleware, metrics=metrics)
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        with TestClient(app) as client:
            client.get("/PRIVATE_PATH_SENTINEL")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if any(r.name == "hermes_cli.web_server.route_metrics" for r in caplog.records):
                    break
                time.sleep(.01)
            logs = [r.message for r in caplog.records if r.name == "hermes_cli.web_server.route_metrics"]
            assert len(logs) == 1, "the periodic summary must run without another request"
    assert "unmatched" in logs[0]
    assert "PRIVATE_" not in logs[0]


def test_label_cap_folds_overflow_without_losing_counts(caplog):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.route_metrics import RouteMetrics, RouteMetricsMiddleware

    metrics = RouteMetrics(max_labels=2)
    app = FastAPI()
    app.add_middleware(RouteMetricsMiddleware, metrics=metrics)
    client = TestClient(app)
    for build in range(8):
        client.get("/PRIVATE_PATH_SENTINEL", headers={"User-Agent": f"HermesMobile/{build}"})
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        metrics.emit_summary()
    line = caplog.records[-1].message
    rows = json.loads(line.split("route_metrics ", 1)[1])
    assert len(rows) <= 2
    assert sum(row["count"] for row in rows) == 8
    assert any(row["label"][0] == "other" for row in rows)
    assert "PRIVATE_" not in line
    before = len(caplog.records)
    metrics.emit_summary()
    assert len(caplog.records) == before  # Empty intervals produce no misleading rows.


@pytest.mark.parametrize("agent", ["HermesMobile/123 PRIVATE_UA_SENTINEL", "HermesMobile/１２３",
                                   "HermesMobile/123\nPRIVATE_UA_SENTINEL", "HermesMobile/1234567890",
                                   "PRIVATE_UA_SENTINEL", "HermesDesktop/7", "HermesMobile/123"])
def test_client_labels_accept_only_the_complete_ascii_grammar(agent):
    from hermes_cli.route_metrics import client_label

    label = client_label([(b"user-agent", agent.encode("utf-8"))])
    expected = tuple(agent.split("/")) if agent in ("HermesDesktop/7", "HermesMobile/123") else ("unknown", "unknown")
    assert label == expected
