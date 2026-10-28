"""A server can enter the parked wait before its first transport setup."""


def test_initial_server_has_default_parked_retry_interval():
    from tools import mcp_tool

    server = mcp_tool.MCPServerTask("fixture-server")
    assert server._parked_retry_interval == mcp_tool._resolve_parked_retry_interval({})
