"""Connection failures are distinct from errors after a client has sent work."""
import asyncio
from contextlib import asynccontextmanager
import json

import pytest
from websockets.protocol import State

from hermes_cli import gateway_client, gateway_runtime


@pytest.fixture
def remote_only(monkeypatch):
    monkeypatch.setenv('HERMES_TUI_GATEWAY_URL', 'ws://127.0.0.1:1/api/ws')

    def refuse_local(*args, **kwargs):
        pytest.fail('An explicit gateway must never fall back to a local owner')

    monkeypatch.setattr(gateway_runtime, 'ensure_gateway_runtime', refuse_local)
    monkeypatch.setattr(gateway_client, '_session_ticket', refuse_local)


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [ConnectionRefusedError, TimeoutError],
                         ids=['connection-refused', 'handshake-timeout'])
async def test_initial_connection_failure_is_translated_without_fallback(monkeypatch, remote_only, error_type):
    error = error_type('synthetic connection failure')
    attempts = []

    @asynccontextmanager
    async def connect(url, **kwargs):
        attempts.append((url, kwargs))
        raise error
        yield  # pragma: no cover -- marks a context manager whose entry fails

    monkeypatch.setattr('websockets.asyncio.client.connect', connect)
    with pytest.raises(gateway_client.GatewayClientError) as caught:
        async with gateway_client.connect_gateway():
            pytest.fail('A failed connection yielded a client')
    assert str(caught.value) == 'Gateway connection failed; no local fallback'
    assert caught.value.__cause__ is error
    assert len(attempts) == 1
    assert attempts[0][0] == 'ws://127.0.0.1:1/api/ws'
    assert attempts[0][1]['open_timeout'] == 10
    assert attempts[0][1]['proxy'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [None, TimeoutError, OSError, asyncio.CancelledError,
                                      gateway_client.GatewayClientError],
                         ids=['normal', 'timeout', 'filesystem', 'cancelled', 'client-error'])
async def test_context_body_outcome_is_preserved_and_transport_closes(monkeypatch, remote_only, error_type):
    class Socket:
        state = State.OPEN

        def __init__(self):
            self.inbox = asyncio.Queue()
            self.sent = []

        async def send(self, raw):
            request = json.loads(raw)
            self.sent.append(request)
            await self.inbox.put(json.dumps({'id': request['id'], 'result': {'ok': True}}))

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.inbox.get()

    socket = Socket()
    closed = []
    error = error_type('synthetic body failure') if error_type else None
    client = None

    @asynccontextmanager
    async def connect(url, **kwargs):
        try:
            yield socket
        finally:
            closed.append(client.reader.cancelled())

    monkeypatch.setattr('websockets.asyncio.client.connect', connect)

    async def use_client():
        nonlocal client
        async with gateway_client.connect_gateway() as client:
            assert await client.rpc('runtime.describe') == {'ok': True}
            if error is not None:
                raise error

    if error is None:
        await use_client()
    else:
        with pytest.raises(error_type) as caught:
            await use_client()
        assert caught.value is error
        assert caught.value.__cause__ is None
    assert [request['method'] for request in socket.sent] == ['runtime.describe']
    assert client.pending == {}
    assert client.reader.done() and client.reader.cancelled()
    assert closed == [True]  # reader cancellation finishes before the WebSocket closes
