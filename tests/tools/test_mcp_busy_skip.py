"""``skip_if_busy`` never reaches the server, so it must cost nothing.

A skipped ambient call is not a server failure (no circuit-breaker strike, so the agent's own calls
keep working while a long tool holds the RPC slot) and is not a dispatched call (a costed tool's
booked charge is refunded).
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_tool, mcp_tool_costs
from tools import mcp_tool_handlers as handlers
from tools import mcp_tool_registration as registration
from tools.approval_context import reset_current_session_key, set_current_session_key


def _run_on_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def occupied():
    """Server ``srv`` whose RPC slot is held by another call; release it with ``.release()``."""
    lock = asyncio.Lock()
    asyncio.run(lock.acquire())
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")], isError=False, structuredContent=None))
    with patch.dict(mcp_tool._servers, {"srv": SimpleNamespace(session=session, _rpc_lock=lock)}), \
         patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=_run_on_loop), \
         patch.dict(mcp_tool._server_error_counts, {}, clear=True), \
         patch.dict(mcp_tool._server_breaker_opened_at, {}, clear=True), \
         patch.dict(mcp_tool._server_trust_levels, {}, clear=True), \
         patch.dict(mcp_tool._tool_read_only_hints, {}, clear=True), \
         patch.dict(mcp_tool._server_cost_policies, {}, clear=True), \
         patch.dict(mcp_tool_costs._spent, {}, clear=True), \
         patch.dict(mcp_tool_costs._ask_locks, {}, clear=True), \
         patch.object(mcp_tool_costs, "_unscoped_moved_to", None):
        yield SimpleNamespace(lock=lock, session=session)


def _call(tool, **options):
    asked = []
    token = set_current_session_key("chat-a")
    try:
        with patch("tools.approval_prompt.request_elicitation_consent",
                   side_effect=lambda msg, desc, **kw: asked.append(msg) or "accept"):
            raw = handlers._make_tool_handler("srv", tool, 30.0, **options)({})
    finally:
        reset_current_session_key(token)
    return json.loads(raw), asked


def test_busy_skips_never_open_the_breaker(occupied):
    for _ in range(mcp_tool._CIRCUIT_BREAKER_THRESHOLD):
        assert _call("search", skip_if_busy=True)[0] == {"error": "mcp_busy"}
    assert mcp_tool._server_error_counts.get("srv", 0) == 0
    occupied.lock.release()
    assert _call("search")[0] == {"result": "ok"}


def test_busy_skip_refunds_a_costed_call(occupied):
    registration._record_tool_trust_metadata(
        "srv", {"trust": "full", "tool_costs": {"paid": 40}, "spend_limit": 100},
        [SimpleNamespace(name="paid", annotations=None)])
    for _ in range(2):  # 80 of 100 if the skips were charged
        assert _call("paid", skip_if_busy=True)[0] == {"error": "mcp_busy"}
    occupied.lock.release()
    result, asked = _call("paid")  # 40 of 100 when refunded: trust: full stays silent below the limit
    assert result == {"result": "ok"} and asked == []
    assert occupied.session.call_tool.await_count == 1
