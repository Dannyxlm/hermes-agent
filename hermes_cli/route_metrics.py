"""Bounded, process-local latency/size histograms; never retain request content.

Labels are selected at the transport boundary from the matched server route or
method registry. The GUI logger receives only interval aggregates, not samples.
"""
from __future__ import annotations

from bisect import bisect_left
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import json
import logging
import math
import re
import threading
import time

_LOG = logging.getLogger("hermes_cli.web_server.route_metrics")
_CLIENT = re.compile(r"(HermesMobile|HermesDesktop)/([0-9]{1,9})", re.ASCII)
_DURATION_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)
_BYTES = (0, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216)
_OTHER = ("other", "other", "unknown", "unknown", "other")


def client_label(headers) -> tuple[str, str]:
    """Accept only a whole, bounded product/build token, never a UA substring."""
    for key, value in headers:
        if key.lower() == b"user-agent":
            if len(value) <= 32:
                match = _CLIENT.fullmatch(value.decode("ascii", errors="replace"))
                if match:
                    return match[1], match[2]
            break
    return ("unknown", "unknown")


def _percentile(histogram, bounds, count, fraction):
    rank = math.ceil(count * fraction)
    total = 0
    for index, frequency in enumerate(histogram):
        total += frequency
        if total >= rank:
            # Fixed upper bounds, with an explicit overflow marker (not a fake bound).
            return bounds[index] if index < len(bounds) else "overflow"


class RouteMetrics:
    """One cheap lock per update; bounded storage, fixed histogram buckets."""

    def __init__(self, max_labels=256):
        self.max_labels = max_labels
        self._lock = threading.Lock()
        self._rows = {}

    def record(self, label, duration_ms, output_bytes):
        with self._lock:
            # Reserve the last slot for overflow, so the cap includes `other`.
            if label not in self._rows and len(self._rows) >= self.max_labels - 1:
                label = _OTHER
            row = self._rows.get(label)
            if row is None:
                row = [0, 0, [0] * (len(_DURATION_MS) + 1), [0] * (len(_BYTES) + 1)]
                self._rows[label] = row
            row[0] += 1
            row[1] += output_bytes
            row[2][bisect_left(_DURATION_MS, duration_ms)] += 1
            row[3][bisect_left(_BYTES, output_bytes)] += 1

    def emit_summary(self):
        with self._lock:
            rows, self._rows = self._rows, {}
        if not rows:
            return
        summary = []
        for label, (count, byte_total, durations, sizes) in sorted(rows.items()):
            summary.append({"label": label, "count": count, "bytes_total": byte_total,
                            "p50_ms": _percentile(durations, _DURATION_MS, count, .50),
                            "p95_ms": _percentile(durations, _DURATION_MS, count, .95),
                            "p50_bytes": _percentile(sizes, _BYTES, count, .50),
                            "p95_bytes": _percentile(sizes, _BYTES, count, .95)})
        _LOG.info("route_metrics %s", json.dumps(summary, separators=(",", ":")))


METRICS = RouteMetrics()
_SUMMARY_INTERVAL_S = 180.0
_RPC: ContextVar[_RPCObservation | None] = ContextVar("route_metrics_rpc", default=None)
_CONNECTION: ContextVar[tuple[RouteMetrics, tuple[str, str]] | None] = ContextVar("route_metrics_connection", default=None)
_ENVELOPE_KEY = re.compile(r'\s*[,\{]\s*"(jsonrpc|id|method|result|error)"\s*:\s*')
_DECODER = json.JSONDecoder()


def _response_status(text):
    """Read only the server envelope, never deserialize the potentially huge result.

    serialize_frame preserves envelope insertion order: jsonrpc, id, result/error.
    Events/requests have method instead; their payloads are not scanned or counted.
    """
    offset = 0
    for _ in range(3):
        match = _ENVELOPE_KEY.match(text, offset)
        if not match:
            return None
        key = match[1]
        if key == "method":
            return None
        if key in ("result", "error"):
            return "success" if key == "result" else "error"
        _, offset = _DECODER.raw_decode(text, match.end())
    return None


class _RPCObservation:
    def __init__(self, metrics, method, client):
        self.metrics = metrics
        self.label = ("rpc", method, *client)
        self.started = time.perf_counter()
        self.finished = False

    def sent(self, text):
        if self.finished:
            return
        status = _response_status(text)
        if status is not None:
            self.finished = True
            self.metrics.record((*self.label, status),
                                (time.perf_counter() - self.started) * 1000,
                                len(text) if text.isascii() else len(text.encode("utf-8")))


@contextmanager
def rpc_dispatch_metrics(method, registered_methods):
    """Context propagates through to_thread and the existing pool's copy_context.

    No transport proxy or ID map: both would interfere with transport ownership.
    Only the selected server label survives into the asynchronous response hook.
    """
    connection = _CONNECTION.get()
    if connection is None:
        yield
        return
    metrics, client = connection
    label = method if isinstance(method, str) and method in registered_methods else "unknown_method"
    token = _RPC.set(_RPCObservation(metrics, label, client))
    try:
        yield
    finally:
        _RPC.reset(token)


class RouteMetricsMiddleware:
    """Pure ASGI counting preserves streaming and never buffers HTTP bodies."""

    def __init__(self, app, metrics=METRICS):
        self.app = app
        self.metrics = metrics

    async def _report_periodically(self):
        while True:
            await asyncio.sleep(_SUMMARY_INTERVAL_S)
            await asyncio.to_thread(self.metrics.emit_summary)

    async def _lifespan(self, scope, receive, send):
        reporter = None

        async def lifespan_send(message):
            nonlocal reporter
            if message["type"] == "lifespan.startup.complete":
                reporter = asyncio.create_task(self._report_periodically())
            await send(message)

        try:
            await self.app(scope, receive, lifespan_send)
        finally:
            if reporter is not None:
                reporter.cancel()
                try:
                    await reporter
                except asyncio.CancelledError:
                    pass

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self._lifespan(scope, receive, send)
        if scope["type"] == "websocket":
            token = _CONNECTION.set((self.metrics, client_label(scope.get("headers", ()))))

            async def measured_ws_send(message):
                observation = _RPC.get()
                await send(message)
                if observation is not None and message["type"] == "websocket.send":
                    observation.sent(message.get("text", ""))

            try:
                return await self.app(scope, receive, measured_ws_send)
            finally:
                _CONNECTION.reset(token)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        client = client_label(scope.get("headers", ()))
        status, size = 500, 0

        async def measured_send(message):
            nonlocal status, size
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                size += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None)
            if template is None:
                template = "rejected" if status in (400, 401, 403, 413) else "unmatched"
            label = ("http", template, *client, f"{status // 100}xx")
            self.metrics.record(label, (time.perf_counter() - started) * 1000, size)
