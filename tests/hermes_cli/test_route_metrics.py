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
    assert row["label"] == ["http", "/api/sessions/{id}/messages", "phone", "123", "2xx"]
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
    expected = ["rpc", "metrics.test", "phone", "123", "success"] if known else [
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


_PHONE = "HermesMobile/2026100705 CFNetwork/3896.100.1.2.1 Darwin/27.0.0"
_WIDGET = "HermesLiveActivityWidget/2026100705 CFNetwork/3896.100.1.2.1 Darwin/27.0.0"
_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Hermes/0.0.0 Chrome/140.0.7339.41 Electron/38.1.0 Safari/537.36")


@pytest.mark.parametrize("agent,expected", [
    # Round 9 U31 (R26): a bounded leading token picks the client; the suffix is never read.
    (_PHONE, ("phone", "2026100705")),
    ("HermesMobile/123", ("phone", "123")),
    ("HermesMobile/123 PRIVATE_UA_SENTINEL", ("phone", "123")),
    (_WIDGET, ("widget", "2026100705")),
    (_DESKTOP, ("desktop", "0.0.0")),
    ("HermesDesktop/7", ("desktop", "7")),
    ("HermesMobile/１２３", ("unknown", "unknown")),
    ("HermesMobile/123\nPRIVATE_UA_SENTINEL", ("unknown", "unknown")),
    ("HermesMobile/123\n", ("unknown", "unknown")),
    ("HermesMobile/1234567890123", ("unknown", "unknown")),
    ("PRIVATE_UA_SENTINEL HermesMobile/123", ("unknown", "unknown")),
    ("PRIVATE_UA_SENTINEL", ("unknown", "unknown")),
    ("Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/140.0 Safari/537.36", ("unknown", "unknown")),
    (_PHONE + " " + "x" * 600, ("unknown", "unknown")),
    ("Mozilla/5.0 Hermes/0.0.0" + " PRIVATE_UA_SENTINEL" * 40 + " Electron/38.1.0", ("unknown", "unknown")),
])
def test_client_labels_accept_only_the_bounded_leading_grammar(agent, expected):
    from hermes_cli.route_metrics import client_label

    assert client_label([(b"user-agent", agent.encode("utf-8"))]) == expected


def test_summary_reports_p99_and_marks_overflow_honestly(caplog):
    from hermes_cli.route_metrics import RouteMetrics

    metrics = RouteMetrics()
    label = ("http", "/api/x", "phone", "1", "2xx")
    for _ in range(98):
        metrics.record(label, 3, 10)
    metrics.record(label, 400, 10)
    metrics.record(label, 120_000, 10)  # past the top bucket
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        metrics.emit_summary()
    row = json.loads(caplog.records[-1].message.split("route_metrics ", 1)[1])[0]
    assert (row["p50_ms"], row["p95_ms"], row["p99_ms"]) == (5, 5, 500)

    for _ in range(2):
        metrics.record(label, 120_000, 10)
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        metrics.emit_summary()
    row = json.loads(caplog.records[-1].message.split("route_metrics ", 1)[1])[0]
    assert row["p99_ms"] == "overflow"


@pytest.mark.parametrize("outcome,status", [("ok", 200), ("changed", 409)])
def test_widget_snapshot_served_by_middleware_is_labelled(caplog, monkeypatch, outcome, status):
    from fastapi.testclient import TestClient
    from hermes_cli import route_metrics, web_server
    from tui_gateway import mobile_widget_inbox, server

    class Store:
        def widget_snapshot(self, _token):
            return {"items": []}

    def inbox_snapshot(_store, _token, _cursor):
        raise mobile_widget_inbox.WidgetInboxChanged()

    monkeypatch.setattr(server, "_mobile_push_service", lambda: SimpleNamespace(store=Store()))
    monkeypatch.setattr(mobile_widget_inbox, "inbox_snapshot", inbox_snapshot)
    monkeypatch.setattr(web_server.app.state, "bound_host", None, raising=False)
    route_metrics.METRICS.emit_summary()
    query = "" if outcome == "ok" else "?inbox=1"
    response = TestClient(web_server.app).get(
        "/api/mobile/widgets/snapshot" + query,
        headers={"Authorization": "Bearer PRIVATE_TOKEN_SENTINEL", "User-Agent": _WIDGET})
    assert response.status_code == status
    with caplog.at_level("INFO", logger="hermes_cli.web_server.route_metrics"):
        route_metrics.METRICS.emit_summary()
    logs = [r.message for r in caplog.records if r.name == "hermes_cli.web_server.route_metrics"]
    rows = json.loads(logs[-1].split("route_metrics ", 1)[1])
    assert [row["label"] for row in rows] == [
        ["http", "/api/mobile/widgets/snapshot", "widget", "2026100705", f"{status // 100}xx"]]
    assert "PRIVATE_" not in logs[-1]
