"""MCP failures: the model keeps every server's message; ``ctx.call_mcp`` gets a typed envelope.

The model-facing renderer shows the server's own error text (credential patterns redacted) for every
MCP server. Only the plugin envelope projects failures: its ``error`` text and ``_meta`` notices pass a
generic allowlist, while ``structuredContent`` reaches the plugin unprojected so a backend plugin reads
its own receipt shape.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from hermes_cli.plugins import PluginContext, PluginManifest
from tools import mcp_tool
from tools.mcp_tool_handlers import _render_call_tool_result


def _failure(*blocks, structured=None, meta=None):
    return SimpleNamespace(isError=True, content=[SimpleNamespace(text=b) for b in blocks],
                           structuredContent=structured, meta=meta)


def _run_on_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def call_mcp(monkeypatch):
    """``ctx.call_mcp`` through the real native handler and renderer against one mocked server."""
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config",
                        lambda *a, **k: {"plugins": {"entries": {"backend": {"mcp_allowlist": ["srv"]}}}})
    session = MagicMock()
    server = SimpleNamespace(session=session, _rpc_lock=asyncio.Lock())
    with patch.dict(mcp_tool._servers, {"srv": server}), \
         patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=_run_on_loop), \
         patch.dict(mcp_tool._server_error_counts, {}, clear=True), \
         patch.dict(mcp_tool._server_trust_levels, {}, clear=True), \
         patch.dict(mcp_tool._tool_read_only_hints, {}, clear=True):
        def call(wire):
            session.call_tool = AsyncMock(return_value=wire)
            ctx = PluginContext(PluginManifest(name="backend", key="backend"), MagicMock())
            return ctx.call_mcp("srv", "write")
        yield call


@pytest.mark.parametrize("body, message", [
    ({"error": "File not found: /tmp/x.txt"}, "File not found: /tmp/x.txt"),
    ({"message": "rate limited, retry in 30s", "code": 429}, "rate limited, retry in 30s"),
])
def test_model_sees_the_message_of_a_json_error_body(body, message):
    rendered = json.loads(_render_call_tool_result(_failure(json.dumps(body)), "fs"))
    assert message in rendered["error"]


def test_model_facing_error_still_redacts_credentials():
    rendered = json.loads(_render_call_tool_result(_failure('{"error": "rejected Bearer abc123"}'), "fs"))
    assert "abc123" not in rendered["error"]


def test_plugin_receives_the_raw_receipt_and_generically_projected_text_and_notices(call_mcp):
    secret = uuid4().hex
    receipt = {"error": "pending_write", "job": {"id": "op-1", "phase": "queued"}, "debug": [secret]}
    envelope = call_mcp(_failure(json.dumps({"error": "pending_write", "detail": secret}),
                                 structured=receipt,
                                 meta={"com.example/notice": "degraded",
                                       "com.example/notices": [{"code": "capacity_low", "why": secret}],
                                       "com.example/debug": secret, "notices": [{"code": "Not A Code"}]}))
    assert envelope["ok"] is False
    assert json.loads(envelope["error"]) == {"error": "pending_write"}
    assert envelope["structuredContent"] == receipt
    assert envelope["_meta"] == {"com.example/notice": "degraded",
                                 "com.example/notices": [{"code": "capacity_low"}], "notices": []}


def test_plugin_error_text_drops_a_json_block_behind_a_prose_prefix(call_mcp):
    secret = uuid4().hex
    envelope = call_mcp(_failure("Error: ", json.dumps({"code": "not_found", "trace": secret})))
    assert secret not in json.dumps(envelope)
    assert envelope["error"] == 'Error: {"code": "not_found"}'


def test_plugin_error_text_without_typed_fields_is_a_generic_failure(call_mcp):
    envelope = call_mcp(_failure(json.dumps({"message": "rate limited, retry in 30s"})))
    assert envelope["error"] == "MCP tool returned an error"


def test_plugin_failure_metadata_honors_server_meta_exclude(call_mcp):
    with patch("tools.mcp_tool_handlers._calling_meta_exclude", return_value=frozenset({"vendor/notice"})):
        envelope = call_mcp(_failure("pending", meta={"vendor/notice": "pending", "vendor/warnings": ["degraded"]}))
    assert envelope["_meta"] == {"vendor/warnings": ["degraded"]}


def test_error_envelope_projects_untrusted_fields_even_without_renderer():
    secret = uuid4().hex
    raw = json.dumps({"error": json.dumps({"error": "denied", "token": secret}),
                      "structuredContent": {"error": "denied"}, "_meta": {"vendor/debug": [secret]}})
    result = PluginContext._mcp_envelope(raw)
    assert secret not in json.dumps(result)
    assert json.loads(result["error"]) == {"error": "denied"}
    assert result["structuredContent"] == {"error": "denied"}
    assert not result.get("_meta")
