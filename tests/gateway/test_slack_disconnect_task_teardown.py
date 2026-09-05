"""Exercise the real SDK's detached disconnect-message reconnect path."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

from gateway.config import PlatformConfig
from plugins.platforms.slack import adapter as slack_adapter


class UnreachableTransport:
    def __init__(self):
        self.closed = False
        self.attempted = asyncio.Event()
        self.connect_tasks = set()
        self.attempts_after_close = 0

    async def ws_connect(self, *args, **kwargs):
        self.connect_tasks.add(asyncio.current_task())
        self.attempted.set()
        if self.closed:
            self.attempts_after_close += 1
            raise RuntimeError("Session is closed")
        raise ConnectionError("synthetic unreachable transport")

    async def close(self):
        self.closed = True


@pytest_asyncio.fixture
async def real_sdk_adapter(monkeypatch):
    logger = logging.getLogger("test.slack.disconnect-teardown")
    monkeypatch.setattr(logger, "disabled", True)
    client = SocketModeClient(
        app_token="xapp-test", web_client=AsyncWebClient(token="xoxb-test"),
        ping_interval=0.01, logger=logger,
    )
    await client.aiohttp_client_session.close()
    transport = UnreachableTransport()
    client.aiohttp_client_session = transport

    async def issue_url():
        return "wss://localhost.invalid/test"

    client.issue_new_wss_url = issue_url
    handler = SimpleNamespace(
        client=client, start_async=asyncio.Event().wait, close_async=client.close,
    )
    monkeypatch.setattr(slack_adapter, "AsyncSocketModeHandler", lambda *a, **kw: handler)
    adapter = slack_adapter.SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._app = object()
    adapter._app_token = "xapp-test"
    adapter._proxy_url = None
    adapter._start_socket_mode_handler()
    main_task = adapter._socket_mode_task
    try:
        yield adapter, client, transport
    finally:
        await adapter._stop_socket_mode_handler()
        tasks = transport.connect_tasks | {main_task, client.message_processor}
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_detached_disconnect_reconnect_stops_before_transport_closes(real_sdk_adapter):
    adapter, client, transport = real_sdk_adapter
    # process_message() launches run_message_listeners() with ensure_future;
    # that task is absent from the SDK's three background-task attributes.
    await client.enqueue_message('{"type":"disconnect"}')
    await asyncio.wait_for(transport.attempted.wait(), timeout=2)
    reconnect_tasks = set(transport.connect_tasks)

    await adapter._stop_socket_mode_handler()

    assert all(task.done() for task in reconnect_tasks)
    assert transport.attempts_after_close == 0


@pytest.mark.asyncio
async def test_disconnect_scheduled_during_teardown_cannot_restart_closed_client(real_sdk_adapter):
    adapter, client, transport = real_sdk_adapter
    queued = asyncio.create_task(client.run_message_listeners(
        {"type": "disconnect"}, '{"type":"disconnect"}',
    ))
    try:
        await adapter._stop_socket_mode_handler()
        await asyncio.wait_for(queued, timeout=2)
        assert not transport.connect_tasks
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)


@pytest.mark.asyncio
async def test_normal_message_listener_survives_transport_teardown(real_sdk_adapter):
    adapter, client, _ = real_sdk_adapter
    started, release = asyncio.Event(), asyncio.Event()

    async def message_listener(*args):
        started.set()
        await release.wait()

    client.message_listeners.append(message_listener)
    message = asyncio.create_task(client.run_message_listeners(
        {"type": "events_api"}, '{"type":"events_api"}',
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await adapter._stop_socket_mode_handler()
        assert not message.done()
        release.set()
        await asyncio.wait_for(message, timeout=2)
    finally:
        message.cancel()
        await asyncio.gather(message, return_exceptions=True)
