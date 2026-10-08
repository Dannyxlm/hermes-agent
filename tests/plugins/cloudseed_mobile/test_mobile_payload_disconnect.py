"""A phone hang-up while the dashboard reads a mobile JSON body is quiet (round 9 U30, R25).

Drives the real router through raw ASGI so the client can disconnect mid-body,
which TestClient cannot do. Synthetic fixtures only.
"""
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI

from plugins.cloudseed_mobile.dashboard import plugin_api as api

P = '/api/plugins/cloudseed_mobile'


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'owner', 'owner_provider': 'basic'})
    application = FastAPI()

    @application.middleware('http')
    async def session(request, call_next):
        if request.headers.get('authorization') == 'Bearer fixture':
            request.state.session = SimpleNamespace(user_id='owner', provider='basic')
        return await call_next(request)

    from hermes_cli.web_server_dashboard import _plugin_route_secret_scope
    application.include_router(api.router, prefix=P, dependencies=[Depends(_plugin_route_secret_scope)])
    return application


async def call(app, path, chunks, *, disconnect=False, auth=True):
    """POST ``chunks`` as a streamed body; end with a client disconnect when asked."""
    headers = [(b'content-type', b'application/json')]
    if auth:
        headers.append((b'authorization', b'Bearer fixture'))
    scope = {
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST',
        'scheme': 'http', 'path': P + path, 'raw_path': (P + path).encode(), 'query_string': b'',
        'root_path': '', 'headers': headers, 'client': ('127.0.0.1', 1), 'server': ('testserver', 80),
    }
    messages = [{'type': 'http.request', 'body': chunk, 'more_body': True} for chunk in chunks]
    if disconnect:
        messages.append({'type': 'http.disconnect'})
    else:
        messages.append({'type': 'http.request', 'body': b'', 'more_body': False})
    sent = []

    async def receive():
        if messages:
            return messages.pop(0)
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m['type'] == 'http.response.start')
    return start['status']


@pytest.mark.asyncio
async def test_mid_body_disconnect_is_quiet_and_not_5xx(app, caplog):
    caplog.set_level(logging.DEBUG)
    status = await call(app, '/memory/write', [b'{"section": "soul", "con'], disconnect=True)
    assert status == 499
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_disconnect_before_any_body_is_quiet(app, caplog):
    caplog.set_level(logging.DEBUG)
    status = await call(app, '/flight-recorder/batch', [], disconnect=True)
    assert status == 499
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_body_limits_and_auth_are_unchanged(app):
    big = b'x' * (api.MAX_BODY // 4)
    assert await call(app, '/memory/write', [big] * 5) == 413
    assert await call(app, '/memory/write', [b'{"section":']) == 400
    assert await call(app, '/memory/write', [b'[1, 2]']) == 400
    body = json.dumps({'section': 'soul', 'content': 'fixture'}).encode()
    assert await call(app, '/memory/write', [body], auth=False) == 401
    assert await call(app, '/memory/write', [body]) == 200
