"""Local bootstrap scope remains an explicit client request, never a remote URL hint."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_client, gateway_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('discovered', [False, True])
async def test_local_scope_and_subject_reach_private_ticket_issuer(monkeypatch, discovered):
    endpoint = SimpleNamespace(api_origin='http://127.0.0.1:1', profile_id='/fixture', instance_id='instance')
    requested = []
    def ticket(home, target, **params):
        requested.append((target, params))
        return 'fixture-ticket'
    class Socket:
        subprotocol = gateway_client.GATEWAY_WS_PROTOCOL
        def __aiter__(self):
            return self
        async def __anext__(self):
            await asyncio.Event().wait()
    @asynccontextmanager
    async def connect(url, **params):
        yield Socket()
    monkeypatch.delenv('HERMES_TUI_GATEWAY_URL', raising=False)
    monkeypatch.setattr(gateway_client, '_session_ticket', ticket)
    monkeypatch.setattr(gateway_runtime, 'ensure_gateway_runtime', lambda *a, **k: SimpleNamespace(endpoint=endpoint))
    monkeypatch.setattr(gateway_runtime, 'attachable', lambda result: True)
    monkeypatch.setattr('websockets.asyncio.client.connect', connect)
    subject = 'auth:v1:["basic","","fixture-owner"]'
    async with gateway_client.connect_gateway(endpoint=endpoint if discovered else None, subject=subject, scope='host'):
        pass
    assert requested == [(endpoint, {'subject': subject, 'scope': 'host'})]


@pytest.mark.asyncio
async def test_bootstrap_scope_cannot_be_requested_via_remote_url(monkeypatch):
    monkeypatch.setenv('HERMES_TUI_GATEWAY_URL', 'wss://example.invalid/gateway')
    with pytest.raises(gateway_client.GatewayClientError, match='local gateway endpoint'):
        async with gateway_client.connect_gateway(scope='host'):
            pytest.fail('A remote scope request was accepted')
