"""A recovery retry preserves the distinction between caller and server deadlines."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp.client.auth import OAuthFlowError

from tools import mcp_tool, mcp_tool_handlers as handlers, mcp_tool_loop


@pytest.mark.parametrize("recovery", ["session", "auth", "stdio", "auth-refresh"])
@pytest.mark.parametrize("deadline", ["ambient", "foreground", "remote"])
def test_retry_deadline_keeps_its_type_and_breaker_policy(monkeypatch, recovery, deadline):
    server = SimpleNamespace(session=object(), _reconnect_event=object())
    monkeypatch.setattr(handlers, "_lookup_reconnectable_server", lambda *a, **kw: server)
    monkeypatch.setattr(handlers, "_mcp_loop_running", lambda: True)
    monkeypatch.setattr(mcp_tool_loop, "_signal_reconnect_and_wait", lambda *a, **kw: True)
    timeout = (TimeoutError("remote deadline") if deadline == "remote" else
               mcp_tool_loop._CallerBudgetTimeout("caller deadline"))
    recoverer, error = {
        "session": (handlers._handle_session_expired_and_retry, RuntimeError("session not found")),
        "auth": (handlers._handle_auth_error_and_retry, OAuthFlowError("401 Unauthorized")),
        "auth-refresh": (handlers._handle_auth_error_and_retry, OAuthFlowError("401 Unauthorized")),
        "stdio": (handlers._handle_stdio_child_exited_and_retry,
                  handlers._StdioChildExited("child exited", in_flight=False)),
    }[recovery]
    outcomes = [error, True, timeout] if recovery == "auth" else [error, timeout]
    with patch.dict(mcp_tool._server_error_counts, {"fixture": 1}, clear=True), \
         patch.dict(mcp_tool._server_breaker_opened_at, {"fixture": 123.0}, clear=True), \
         patch.dict(mcp_tool._server_errors_all_application, {"fixture": False}, clear=True), \
         patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=outcomes) as run:
        result = handlers._dispatch(
            "fixture", server, "tools/call search", lambda: None, 2, (recoverer,),
            lambda exc: mcp_tool._bump_server_error("fixture"),
            record_outcome=True, caller_timeout_neutral=deadline != "foreground",
        )
        payload = json.loads(result)
        assert run.call_count == len(outcomes)
        if deadline == "ambient":
            assert (payload, mcp_tool._server_error_counts["fixture"]) == ({"error": "mcp_timeout"}, 1)
            assert mcp_tool._server_breaker_opened_at["fixture"] == 123.0
            assert mcp_tool._server_errors_all_application["fixture"] is False
        else:
            assert payload["error"] != "mcp_timeout"
            # OAuth recovery historically clears stale strikes on a completed retry;
            # an uncompleted caller deadline is not evidence of a healthy server.
            expected = 1 if recovery == "auth" and deadline == "remote" else 2
            assert mcp_tool._server_error_counts["fixture"] == expected
