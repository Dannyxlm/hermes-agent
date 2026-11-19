"""Refresh diagnostics identify the configured server without exposing OAuth payloads."""

import logging

import httpx
import pytest
from mcp.shared.auth import OAuthToken

from tools import mcp_oauth
from tools.mcp_oauth_manager import MCPOAuthManager


@pytest.fixture
def provider(tmp_path, monkeypatch):
    # Upstream v0.23.2 deleted the unreached build_oauth_auth path (dd478aa3f3); the manager is the live one.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mcp_oauth, "_is_interactive", lambda: True)
    result = MCPOAuthManager().get_or_build_provider(
        "sample-server", "https://mcp.example.com", {}
    )
    assert result is not None
    result.context.current_tokens = OAuthToken(
        access_token="synthetic-access-secret", token_type="Bearer",
        refresh_token="synthetic-refresh-secret", expires_in=3600,
    )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,failure", [
    (400, b'{"error":"synthetic-response-secret"}', "http_error"),
    (201, b'{"refresh_token":"synthetic-response-secret"}', "invalid_response"),
])
async def test_refresh_failure_identifies_server_without_payload(
    provider, caplog, status, body, failure,
):
    response = httpx.Response(status, content=body, request=httpx.Request(
        "POST", "https://idp.example.com/token?secret=synthetic-url-secret",
    ))
    with caplog.at_level(logging.WARNING):
        assert await provider._handle_refresh_response(response) is False
    assert provider.context.current_tokens is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("server=sample-server" in text and f"status={status}" in text
               and f"failure={failure}" in text for text in messages)
    assert all("synthetic-" not in text and "https://" not in text for text in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [None, "unsafe\nforged=record", "https://private.example/token", "x" * 129])
async def test_unusable_storage_label_is_not_logged(provider, caplog, label):
    provider.context.storage._server_name = label
    response = httpx.Response(400, content=b"not read")
    with caplog.at_level(logging.WARNING):
        assert await provider._handle_refresh_response(response) is False
    messages = [record.getMessage() for record in caplog.records]
    assert any("server=unknown" in text and "failure=http_error" in text for text in messages)
    assert all("forged" not in text and "private.example" not in text and "x" * 129 not in text
               for text in messages)
