"""The compute host starts MCP discovery before it builds an agent. The isolated turn process runs
neither the dashboard's nor the stdio entry's MCP startup, so a build that skips it leaves every MCP
tool and every ``ctx.call_mcp`` consumer (memory recall) unavailable for the life of the process."""

from __future__ import annotations

import io
import types

from tui_gateway import entry, server
from tui_gateway.compute_host import ComputeHost


def _build(monkeypatch, events, *, discovery):
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)

    def fake_make_agent(sid, key, **kwargs):
        events.append("build")
        return types.SimpleNamespace(session_id=key)

    def fake_init_session(sid, key, agent, history, **kwargs):
        monkeypatch.setitem(server._sessions, sid, {"agent": agent, "session_key": key})

    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", discovery)
    monkeypatch.setattr(server, "_make_agent", fake_make_agent)
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda agent, db: False)
    monkeypatch.setattr(server, "_init_session", fake_init_session)
    try:
        host._build_server_session(server, {"sid": "s1", "session_key": "k1", "history": []}, "s1")
    finally:
        host.close()
        server._sessions.pop("s1", None)


def test_session_build_starts_mcp_discovery_before_the_agent(monkeypatch):
    events = []
    _build(monkeypatch, events, discovery=lambda: events.append("discovery"))
    assert events == ["discovery", "build"]


def test_session_build_survives_a_discovery_start_failure(monkeypatch):
    events = []

    def boom():
        raise RuntimeError("discovery could not start")

    _build(monkeypatch, events, discovery=boom)
    assert events == ["build"]
