"""Structured MCP failures expose typed receipts, not server diagnostics."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hermes_cli.plugins import PluginContext
from tools.mcp_tool_handlers import _render_call_tool_result


@pytest.mark.parametrize("text_json", [False, True, "multiblock"])
def test_error_renderer_and_plugin_drop_opaque_diagnostics(text_json):
    secret = uuid4().hex
    receipt = {"request_id": "fixture-operation", "state": "pending", "retry_after_ms": 1000}
    payload = {"error": "pending_write", "write_request": receipt,
               "diagnostics": {"api_key": secret, "nested": [{"password": secret, "token": secret}]},
               "debug": [{"unfamiliar": secret}]}
    wire = SimpleNamespace(isError=True,
        content=[SimpleNamespace(text=json.dumps(payload) if text_json else "pending")],
        structuredContent=payload,
        meta={"vendor/debug": {"access_token": secret}, "unknown": secret,
              "vendor/notice": "pending", "vendor/warnings": ["degraded"],
              "gbrain_notices": [{"code": "held_files", "why": secret}],
              "mcp.io/internal": secret})
    if text_json == "multiblock":
        wire.content.insert(0, SimpleNamespace(text="pending"))
    raw = _render_call_tool_result(wire, "fixture")
    envelope = PluginContext._mcp_envelope(raw)
    assert secret not in raw
    assert secret not in json.dumps(envelope)
    assert envelope["ok"] is False
    assert envelope["structuredContent"] == {"error": "pending_write", "write_request": receipt}
    assert envelope["_meta"] == {"vendor/notice": "pending", "vendor/warnings": ["degraded"],
                                 "gbrain_notices": [{"code": "held_files"}]}


def test_error_envelope_projects_untrusted_fields_even_without_renderer():
    secret = uuid4().hex
    raw = json.dumps({"error": "pending", "structuredContent": {
        "error": "pending_write", "write_request": {"request_id": "op", "state": "pending",
            "retry_after_ms": {"token": secret}, "diagnostics": [{"password": secret}]},
        "diagnostics": [secret]}, "_meta": {"vendor/debug": [secret]}})
    result = PluginContext._mcp_envelope(raw)
    assert secret not in json.dumps(result)
    assert result["structuredContent"] == {"error": "pending_write", "write_request": {
        "request_id": "op", "state": "pending"}}
    assert not result.get("_meta")
