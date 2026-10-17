"""Memory providers retain the native client's grant and wire result contracts."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli.plugins import PluginContext
from plugins.memory import _ProviderCollector
from tools.mcp_tool_handlers import _render_call_tool_result


def test_collector_forwards_native_calls_without_swallowing_denials():
    collector = _ProviderCollector("native-memory")
    seen = []

    def call(server, tool, arguments=None, **kwargs):
        seen.append((server, tool, arguments, kwargs))
        raise PermissionError("grant denied")

    collector._context = SimpleNamespace(call_mcp=call)
    with pytest.raises(PermissionError, match="grant denied"):
        collector.call_mcp("brain", "search", {"query": "seed"}, timeout=2)
    assert seen == [("brain", "search", {"query": "seed"}, {"timeout": 2})]


def test_envelope_preserves_metadata_and_typed_pending_write():
    payload = {"error": "pending_write", "write_request": {"request_id": "operation", "state": "pending"}}
    raw = json.dumps({"error": "pending", "structuredContent": payload,
                      "_meta": {"vendor/warnings": ["degraded"]}})
    result = PluginContext._mcp_envelope(raw)
    assert result == {"ok": False, "error": "pending", "structuredContent": payload,
                      "_meta": {"vendor/warnings": ["degraded"]}}


def test_oversized_json_never_becomes_successful_unparseable_text():
    result = PluginContext._mcp_envelope(json.dumps({"error": "denied", "structuredContent": {"body": "x" * 70000}}))
    assert result["ok"] is False
    assert result["truncated"] is True
    assert result["error"] == "mcp_result_too_large"


def test_native_renderer_preserves_typed_error_data_and_public_metadata():
    payload = {"error": "pending_write", "write_request": {"state": "pending", "request_id": "op"}}
    wire = SimpleNamespace(isError=True, content=[SimpleNamespace(text=json.dumps(payload))],
                           structuredContent=payload, meta={"vendor/notice": "pending", "mcp.io/internal": "hidden"})
    result = PluginContext._mcp_envelope(_render_call_tool_result(wire, "brain"))
    assert result["ok"] is False
    assert result["structuredContent"] == payload
    assert result["_meta"] == {"vendor/notice": "pending"}


def test_isolated_memory_provider_uses_parent_native_grants_and_state(tmp_path, monkeypatch):
    import os
    import hermes_yaml as yaml
    from hermes_cli import plugins as plugins_mod
    from plugins.memory import load_memory_provider
    from tools import mcp_tool_handlers

    home = tmp_path / "home"
    provider_dir = home / "plugins" / "nativeprobe"
    provider_dir.mkdir(parents=True)
    config = {"memory": {"provider": "nativeprobe"}, "plugins": {"isolation": "host", "entries": {
        "nativeprobe": {"mcp_allowlist": ["brain"], "settings": {"native": {"mode": "fixture"}}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    (provider_dir / "__init__.py").write_text('''
import json, os
from agent.memory_provider import MemoryProvider
class Probe(MemoryProvider):
    name = "nativeprobe"
    def __init__(self, ctx): self.ctx = ctx
    def is_available(self): return True
    def initialize(self, session_id, **kwargs): pass
    def get_tool_schemas(self): return []
    def prefetch(self, query, **kwargs):
        self.ctx.state.set("operation", "durable-reference")
        return json.dumps({"pid": os.getpid(), "state": self.ctx.state.get("operation"),
            "config": self.ctx.get_config("native"), "native": self.ctx.call_mcp("brain", "search", {"query": query})})
def register(ctx): ctx.register_memory_provider(Probe(ctx))
''')
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setattr(mcp_tool_handlers, "_make_tool_handler", lambda *args, **kwargs:
                        lambda payload: json.dumps({"result": "text", "_meta": {"vendor/notice": "fixture"}}))
    manager = plugins_mod.get_plugin_manager()
    host = manager._plugin_host()
    try:
        provider = load_memory_provider("nativeprobe")
        result = json.loads(provider.prefetch("question"))
        assert result["pid"] != os.getpid()
        assert result["state"] == "durable-reference"
        assert result["config"] == {"mode": "fixture"}
        assert result["native"]["_meta"] == {"vendor/notice": "fixture"}
        config["plugins"]["entries"]["nativeprobe"]["mcp_allowlist"] = []
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        with pytest.raises(Exception, match="not allowed"):
            provider.prefetch("denied")
    finally:
        host.shutdown()


def test_ambient_native_call_skips_an_occupied_rpc_slot(monkeypatch):
    import asyncio
    from tools import mcp_tool_handlers as handlers
    from tools import mcp_tool

    async def exercise():
        lock = asyncio.Lock()
        await lock.acquire()
        server = SimpleNamespace(_rpc_lock=lock)
        monkeypatch.setattr(handlers, "_trust_gate_check", lambda *a: None)
        monkeypatch.setattr(handlers, "_check_circuit_breaker", lambda *a: None)
        monkeypatch.setattr(handlers, "_acquire_call_server", lambda *a: (server, None))
        monkeypatch.setattr(handlers, "_tool_is_read_only", lambda *a: True)
        async def native(*a, **kw):
            pytest.fail("busy ambient call reached native transport")
        monkeypatch.setattr(handlers, "_call_tool_racing_stdio_death", native)
        def dispatch(_name, _server, _op, call, *_args, **_kwargs):
            return call()
        monkeypatch.setattr(handlers, "_dispatch", dispatch)
        result = await handlers._make_tool_handler("brain", "search", 2, skip_if_busy=True)({"query": "seed"})
        assert json.loads(result)["error"] == "mcp_busy"
        assert lock.locked()
        lock.release()
    asyncio.run(exercise())
