"""Tests for MCP tool structuredContent preservation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_tool
from tools import mcp_tool_content as _mcp_content
from tools import mcp_tool_handlers as _mcp_handlers

class _FakeContentBlock:
    """Minimal content block with .text and .type attributes."""

    def __init__(self, text: str, block_type: str = "text"):
        self.text = text
        self.type = block_type

class _FakeCallToolResult:
    """Minimal CallToolResult stand-in.

    Uses camelCase ``structuredContent`` / ``isError`` to match the real
    MCP SDK Pydantic model (``mcp.types.CallToolResult``).
    """

    def __init__(self, content, is_error=False, structuredContent=None, meta=None):
        self.content = content
        self.isError = is_error
        self.structuredContent = structuredContent
        # Real SDK exposes the wire ``_meta`` field as ``.meta`` (Pydantic alias).
        self.meta = meta

def _fake_run_on_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    """Run an MCP coroutine directly in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        # `_rpc_lock` must be created inside the loop that awaits it, or asyncio
        # raises "attached to a different loop". Build it here and attach it to
        # whatever fake server is currently registered under _servers.
        async def _install_lock_and_run():
            for srv in list(mcp_tool._servers.values()):
                if getattr(srv, "_rpc_lock", None) is None:
                    srv._rpc_lock = asyncio.Lock()
            return await coro
        return loop.run_until_complete(_install_lock_and_run())
    finally:
        loop.close()

@pytest.fixture
def _patch_mcp_server():
    """Patch _servers and the MCP event loop so _make_tool_handler can run."""
    fake_session = MagicMock()
    # `_rpc_lock` is acquired by _make_tool_handler's call path (mcp_tool.py
    # ~L2008) to serialize JSON-RPC against the server — build it inside the
    # fresh loop that _fake_run_on_mcp_loop spins up, not at fixture import.
    fake_server = SimpleNamespace(session=fake_session, _rpc_lock=None)
    with patch.dict(mcp_tool._servers, {"test-server": fake_server}), \
         patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=_fake_run_on_mcp_loop):
        yield fake_session

class TestStructuredContentPreservation:
    """Ensure structuredContent from CallToolResult is forwarded."""

    def test_text_only_result(self, _patch_mcp_server):
        """When no structuredContent, result is text-only (existing behaviour)."""
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("hello")],
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        raw = handler({})
        data = json.loads(raw)
        assert data == {"result": "hello"}

    def test_structured_content_none_falls_back_to_text(self, _patch_mcp_server):
        """When structuredContent is explicitly None, fall back to text."""
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                structuredContent=None,
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        raw = handler({})
        data = json.loads(raw)
        assert data == {"result": "done"}

    def test_empty_text_with_structured_content(self, _patch_mcp_server):
        """When content blocks are empty but structuredContent exists."""
        session = _patch_mcp_server
        payload = {"status": "ok", "data": [1, 2, 3]}
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[],
                structuredContent=payload,
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        raw = handler({})
        data = json.loads(raw)
        assert data["result"] == payload

class TestMetaPassthrough:
    """Server ``_meta`` is surfaced, minus protocol-reserved keys.

    Ported from MoonshotAI/kimi-code#2596/#2600.
    """

    def test_vendor_meta_passes_through(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta={"com.example/handoff": {"url": "https://x"}},
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["result"] == "done"
        assert data["_meta"] == {"com.example/handoff": {"url": "https://x"}}

    def test_reserved_meta_keys_dropped(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta={
                    "modelcontextprotocol.io/progress": 1,
                    "tools.mcp.com/trace": "x",
                    "com.example.mcp/vendor": "keep",  # trailing mcp label = vendor ns
                    "unprefixed": "keep",
                },
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["_meta"] == {
            "com.example.mcp/vendor": "keep",
            "unprefixed": "keep",
        }

    def test_all_reserved_meta_omits_field(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta={"mcp.io/internal": True},
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {"result": "done"}

    def test_meta_with_structured_content(self, _patch_mcp_server):
        """Non-verbatim text keeps structuredContent; _meta still rides."""
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("txt")],
                structuredContent={"ok": True},
                meta={"com.example/k": "v"},
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {
            "result": "txt",
            "structuredContent": {"ok": True},
            "_meta": {"com.example/k": "v"},
        }

    def test_non_serializable_meta_dropped(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta={"com.example/obj": object()},
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {"result": "done"}

    def test_non_dict_meta_ignored(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta="not-a-dict",
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {"result": "done"}

class TestMetaExclude:
    """A server's ``meta_exclude`` names exact ``_meta`` keys kept out of its tool results.

    Configured through the discovery pass (``register_mcp_servers``), like every per-server
    policy; the connected server object carries the config it connected with."""

    @staticmethod
    def _configure(servers: dict, connect: bool = False) -> None:
        from tools import mcp_tool_discovery
        if connect:  # what a connected server carries: its name, tools and the config it ran with
            for name, cfg in servers.items():
                server = mcp_tool._servers[name]
                server.name, server._tools, server._config = name, [], cfg
        mcp_tool_discovery.register_mcp_servers(servers)

    @staticmethod
    def _result(meta, **kw):
        return _FakeCallToolResult(content=[_FakeContentBlock(kw.pop("text", "done"))], meta=meta, **kw)

    @pytest.fixture(autouse=True)
    def _discovery_seams(self, monkeypatch):
        from tools import mcp_tool_config
        monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
        monkeypatch.setattr(mcp_tool_config, "_filter_suspicious_mcp_servers", lambda servers: servers)
        with patch.dict(mcp_tool._server_meta_exclude, {}, clear=True):
            yield

    def _call(self, session, meta, server="test-server", **kw):
        session.call_tool = AsyncMock(return_value=self._result(meta, **kw))
        return json.loads(_mcp_handlers._make_tool_handler(server, "my-tool", 30.0)({}))

    def test_drops_named_keys_and_keeps_the_rest(self, _patch_mcp_server):
        self._configure({"test-server": {"meta_exclude": ["ambient_fact"]}}, connect=True)
        data = self._call(_patch_mcp_server, {"ambient_fact": {"items": [1]}, "call_detail": {"rows": 3}})
        assert data == {"result": "done", "_meta": {"call_detail": {"rows": 3}}}

    def test_excluding_every_key_omits_the_field(self, _patch_mcp_server):
        self._configure({"test-server": {"meta_exclude": ["ambient_fact"]}}, connect=True)
        assert self._call(_patch_mcp_server, {"ambient_fact": {}}) == {"result": "done"}

    def test_match_is_exact_never_substring(self, _patch_mcp_server):
        self._configure({"test-server": {"meta_exclude": ["fact"]}}, connect=True)
        data = self._call(_patch_mcp_server, {"fact": 1, "ambient_fact": 2, "facts": 3})
        assert data["_meta"] == {"ambient_fact": 2, "facts": 3}

    @pytest.mark.parametrize("malformed", [
        "ambient_fact", {"ambient_fact": True}, None, 7, True, [["ambient_fact"]], [{"ambient_fact": 1}]])
    def test_malformed_values_exclude_nothing(self, _patch_mcp_server, malformed):
        """Only a list of strings counts; a bare string must not turn into substring matching."""
        self._configure({"test-server": {"meta_exclude": malformed}}, connect=True)
        data = self._call(_patch_mcp_server, {"ambient_fact": 1, "a": 2})
        assert data["_meta"] == {"ambient_fact": 1, "a": 2}

    def test_reserved_keys_stay_stripped(self, _patch_mcp_server):
        self._configure({"test-server": {"meta_exclude": ["ambient_fact"]}}, connect=True)
        data = self._call(_patch_mcp_server, {
            "modelcontextprotocol.io/progress": 1, "ambient_fact": 2, "com.example/k": 3})
        assert data["_meta"] == {"com.example/k": 3}

    def test_content_and_structured_content_are_untouched(self, _patch_mcp_server):
        """Exclusion applies to ``_meta`` keys only: the same name in text or structuredContent rides."""
        self._configure({"test-server": {"meta_exclude": ["ambient_fact"]}}, connect=True)
        data = self._call(_patch_mcp_server, {"ambient_fact": 1}, text="ambient_fact: see data",
                          structuredContent={"ambient_fact": "payload", "rows": 2})
        assert data == {"result": "ambient_fact: see data",
                        "structuredContent": {"ambient_fact": "payload", "rows": 2}}

    def test_each_server_keeps_its_own_list(self, _patch_mcp_server):
        other = SimpleNamespace(session=MagicMock(), _rpc_lock=None)
        with patch.dict(mcp_tool._servers, {"other-server": other}):
            self._configure({"test-server": {"meta_exclude": ["ambient_fact"]}, "other-server": {}},
                            connect=True)
            meta = {"ambient_fact": 1, "call_detail": 2}
            assert self._call(_patch_mcp_server, meta)["_meta"] == {"call_detail": 2}
            assert self._call(other.session, meta, server="other-server")["_meta"] == meta

    def test_config_edit_applies_on_the_next_pass_without_a_reconnect(self, _patch_mcp_server):
        """The connected server keeps the config it connected with; the next discovery pass re-reads
        ``meta_exclude`` and the very next call honours it, on the same connection."""
        server = mcp_tool._servers["test-server"]
        self._configure({"test-server": {}}, connect=True)
        meta = {"ambient_fact": 1, "call_detail": 2}
        assert self._call(_patch_mcp_server, meta)["_meta"] == meta
        self._configure({"test-server": {"meta_exclude": ["ambient_fact"]}})
        assert self._call(_patch_mcp_server, meta)["_meta"] == {"call_detail": 2}
        self._configure({"test-server": {}})
        assert self._call(_patch_mcp_server, meta)["_meta"] == meta
        assert mcp_tool._servers["test-server"] is server

    def test_session_expired_replay_keeps_the_exclusions(self, _patch_mcp_server, monkeypatch):
        """A read-only call replayed after a session-expiry reconnect renders with the same list."""
        from tools import mcp_tool_loop
        server = mcp_tool._servers["test-server"]
        server._reconnect_event = object()
        monkeypatch.setattr(_mcp_handlers, "_mcp_loop_running", lambda: True)
        monkeypatch.setattr(mcp_tool_loop, "_signal_reconnect_and_wait", lambda *a, **k: True)
        self._configure({"test-server": {"meta_exclude": ["ambient_fact"]}}, connect=True)
        with patch.dict(mcp_tool._tool_read_only_hints, {"test-server": {"my-tool": True}}):
            _patch_mcp_server.call_tool = AsyncMock(side_effect=[
                RuntimeError("session not found"), self._result({"ambient_fact": 1, "call_detail": 2})])
            data = json.loads(_mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)({}))
        assert _patch_mcp_server.call_tool.await_count == 2
        assert data == {"result": "done", "_meta": {"call_detail": 2}}


class TestMetaExcludeKeys:
    def test_list_of_strings(self):
        assert _mcp_content._meta_exclude_keys({"meta_exclude": ["a", "b", ""]}) == frozenset({"a", "b"})

    def test_absent_or_malformed(self):
        assert _mcp_content._meta_exclude_keys(None) == frozenset()
        assert _mcp_content._meta_exclude_keys({}) == frozenset()
        assert _mcp_content._meta_exclude_keys({"meta_exclude": "a"}) == frozenset()
        assert _mcp_content._meta_exclude_keys({"meta_exclude": [1, None, "a"]}) == frozenset({"a"})

class TestReservedMetaKeyPredicate:
    def test_reserved_prefixes(self):
        assert _mcp_content._is_reserved_mcp_meta_key("modelcontextprotocol.io/x")
        assert _mcp_content._is_reserved_mcp_meta_key("mcp.dev/x")
        assert _mcp_content._is_reserved_mcp_meta_key("tools.mcp.com/x")

    def test_vendor_and_unprefixed_not_reserved(self):
        assert not _mcp_content._is_reserved_mcp_meta_key("com.example.mcp/x")  # trailing label
        assert not _mcp_content._is_reserved_mcp_meta_key("com.example/x")
        assert not _mcp_content._is_reserved_mcp_meta_key("plain-key")
        assert not _mcp_content._is_reserved_mcp_meta_key("/leading-slash")

class TestContentStructuredArbitration:
    """Only the spec's verbatim dual-emit is deduplicated (kimi-code#3234, narrowed).

    content being the serialized JSON of structuredContent is a deterministic
    duplicate, so the structured copy is dropped. Any other usable text keeps
    structuredContent alongside it (#115430): servers that return a status
    summary in content were losing their only machine-readable result.
    """

    def test_dual_emit_suppresses_structured(self, _patch_mcp_server):
        """Verbatim dual-emit servers: model receives content only."""
        session = _patch_mcp_server
        payload = {"items": [1, 2, 3]}
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock(json.dumps(payload))],
                structuredContent=payload,
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {"result": json.dumps(payload)}

    def test_dual_emit_forwards_one_copy(self, _patch_mcp_server):
        """The dual-emit check compares parsed JSON per text block: a re-ordered,
        indented, ASCII-escaped copy next to a status line still dedupes. An over-cap
        payload next to usable text is also dropped — forwarding its truncated
        stand-in as well would double the #56059 ceiling."""
        session = _patch_mcp_server
        payload = {"a": "héllo", "b": 2}
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock(json.dumps({"b": 2, "a": "héllo"}, indent=2)), _FakeContentBlock("done")],
                structuredContent=payload,
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert set(data) == {"result"} and data["result"].count('"b"') == 1
        assert data["result"].endswith("done")
        big = {"k": "z" * (_mcp_content._MCP_HARD_RESULT_CAP_CHARS + 10)}
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(content=[_FakeContentBlock("done")], structuredContent=big)
        )
        assert json.loads(handler({})) == {"result": "done"}

    def test_summary_plus_data_keeps_structured(self, _patch_mcp_server):
        """#115430 reproducer shape: a status summary in content with the real data
        (the jid needed by follow-up calls) only in structuredContent. A text-only
        result is unchanged (control)."""
        session = _patch_mcp_server
        structured = {
            "data": [{"jid": "12345@s.whatsapp.net", "name": "Contact"}],
            "pagination": {"offset": 0, "limit": 1},
        }
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("Retrieved 1 chats (offset 0, limit 1)")],
                structuredContent=structured,
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["result"] == "Retrieved 1 chats (offset 0, limit 1)"
        assert data["structuredContent"] == structured
        assert data["structuredContent"]["data"][0]["jid"] == "12345@s.whatsapp.net"
        session.call_tool = AsyncMock(return_value=_FakeCallToolResult(content=[_FakeContentBlock("just text")]))
        assert handler({}) == '{"result": "just text"}'

    def test_whitespace_only_content_falls_back(self, _patch_mcp_server):
        """Whitespace-only text is not usable content — fallback fires."""
        session = _patch_mcp_server
        payload = {"status": "ok"}
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("   \n")],
                structuredContent=payload,
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["structuredContent"] == payload

    def test_structured_only_still_surfaced(self, _patch_mcp_server):
        """structuredContent-only servers keep working (#2596 fix preserved)."""
        session = _patch_mcp_server
        payload = {"only": "structured"}
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[],
                structuredContent=payload,
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["result"] == payload

class TestDroppedBlockNotice:
    """Unsupported content blocks surface a drop notice to the model.

    Ported from MoonshotAI/kimi-code#3227.
    """

    def test_unsupported_block_renders_notice(self, _patch_mcp_server):
        session = _patch_mcp_server
        # NOTE: no `uri` — a uri'd block without .resource is rendered as a
        # resource link by _render_mcp_resource_block, not dropped.
        weird = SimpleNamespace(
            type="hologram",
            mimeType="application/x-hologram",
            size=1234,
        )
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(content=[weird])
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert "[MCP content dropped: unsupported block" in data["result"]
        assert "type=hologram" in data["result"]
        assert "mimeType=application/x-hologram" in data["result"]
        assert "size=1234" in data["result"]

    def test_drop_notice_does_not_suppress_structured(self, _patch_mcp_server):
        """A drop notice is not usable content — structured fallback fires."""
        session = _patch_mcp_server
        weird = SimpleNamespace(type="hologram")
        payload = {"real": "data"}
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[weird], structuredContent=payload,
            )
        )
        handler = _mcp_handlers._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["structuredContent"] == payload
        assert "[MCP content dropped" in data["result"]
