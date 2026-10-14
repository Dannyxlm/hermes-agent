"""Owner-only human-input REST glue; synthetic dispatcher, no network or live stores."""
import asyncio
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from plugins.cloudseed_mobile.dashboard import plugin_api as api

PREFIX = '/api/plugins/cloudseed_mobile/human-input'
OPS = ('pending', 'respond', 'action-status')
SENTINEL = 'fictional-secret-never-log'


def request_body():
    return {'profile': 'default', 'connection_id': 'fixture-connection',
            'installation_id': 'fixture-installation', 'destination_kind': 'session',
            'session_id': 'runtime-fixture', 'stored_session_id': 'stored-fixture',
            'canonical_root_id': 'root-fixture', 'request_id': 'srq-fixture',
            'method': 'secret', 'request_epoch': 'epoch-fixture', 'request_revision': 1,
            'action_id': 'action-fixture', 'result': {'value': SENTINEL}}


@pytest.fixture(params=["in_process", "hosted_mount"])
def client(monkeypatch, tmp_path, request):
    import hermes_constants
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'fixture-owner', 'owner_provider': 'basic'})
    # Avoid the unrelated Files warmer/lifespan and mount with the production scope dependency.
    from hermes_cli.web_server_dashboard import _plugin_route_secret_scope
    app = FastAPI()

    @app.middleware('http')
    async def identity(request, call_next):
        auth = request.headers.get('authorization')
        if auth == 'Bearer fixture-owner' or request.cookies.get('fixture_owner') == '1':
            request.state.session = SimpleNamespace(user_id='fixture-owner', provider='basic')
        elif auth == 'Bearer foreign-owner':
            request.state.session = SimpleNamespace(user_id='foreign', provider='basic')
        elif auth == 'Bearer service-token':
            request.state.token_authenticated = True
        return await call_next(request)

    if request.param == 'hosted_mount':
        from hermes_cli import web_server_dashboard as dashboard
        def forbidden_asgi(*args, **kwargs):
            pytest.fail('Human-input body must not be forwarded into an isolated plugin')
        monkeypatch.setattr(dashboard, '_hosted_plugin_for_request',
                            lambda name: (SimpleNamespace(asgi_request=forbidden_asgi), '/fixture', '/fixture/api.py'))
        dashboard._mount_hosted_plugin_api(app, {'name': 'cloudseed_mobile'}, 'api.py')
    else:
        app.include_router(api.router, prefix='/api/plugins/cloudseed_mobile',
                           dependencies=[Depends(_plugin_route_secret_scope)])
    with TestClient(app) as c:
        c.headers['Authorization'] = 'Bearer fixture-owner'
        yield c


@pytest.fixture
def dispatch(monkeypatch):
    calls = []

    def call(operation, body, *, principal):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        calls.append((operation, body, principal))
        return {'status': 'accepted', 'action_id': body.get('action_id')}

    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, 'tui_gateway.server', SimpleNamespace())
        patch.setitem(sys.modules, 'tui_gateway.methods_human_input', SimpleNamespace(call=call))
        patch.delenv('HERMES_PLUGIN_HOST_PROCESS', raising=False)
        patch.delenv('HERMES_COMPUTE_HOST_CHILD', raising=False)
        yield calls


@pytest.mark.parametrize('operation', OPS)
def test_routes_forward_exact_scope_and_bind_owner(client, dispatch, operation, caplog):
    body = request_body()
    body['principal'] = 'forged-body-owner'
    response = client.post(f'{PREFIX}/{operation}', json=body)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {'status': 'accepted', 'action_id': 'action-fixture'}
    expected = hashlib.sha256(json.dumps(['cloudseed-mobile-owner-v1', 'basic', 'fixture-owner'], separators=(',', ':')).encode()).hexdigest()
    assert dispatch == [(operation, body, expected)]
    assert SENTINEL not in response.text and SENTINEL not in caplog.text


@pytest.mark.parametrize('operation', OPS)
@pytest.mark.parametrize('credential,status', [(None, 401), ('Bearer service-token', 401),
                                             ('Bearer widget-read-token', 401), ('Bearer foreign-owner', 403)])
def test_nonowners_cannot_dispatch(client, dispatch, operation, credential, status):
    client.headers.pop('authorization')
    if credential:
        client.headers['Authorization'] = credential
    assert client.post(f'{PREFIX}/{operation}', json=request_body()).status_code == status
    assert not dispatch


def test_reserved_operations_never_fall_through_to_plugin_host(client, dispatch):
    for path in ('/unknown', ''):
        assert client.post(PREFIX + path, json=request_body()).status_code == 404
    assert client.post(PREFIX + '/respond/', json=request_body(), follow_redirects=False).status_code in (307, 404)
    assert not dispatch


def test_hosted_route_still_requires_enabled_plugin(client, dispatch, monkeypatch):
    from hermes_cli import web_server_dashboard as dashboard
    if not any(route.name == 'native_mobile_pending' for route in client.app.routes):
        pytest.skip('Hosted mount only')
    monkeypatch.setattr(dashboard, '_hosted_plugin_for_request', lambda name: None)
    assert client.post(PREFIX + '/pending', json=request_body()).status_code == 404
    assert not dispatch


def test_cookie_csrf_gate_precedes_dispatch(client, dispatch):
    client.headers.pop('authorization')
    client.cookies.set('fixture_owner', '1')
    for origin in (None, 'https://foreign.invalid'):
        headers = {} if origin is None else {'Origin': origin}
        assert client.post(f'{PREFIX}/respond', json=request_body(), headers=headers).status_code == 403
    assert not dispatch
    assert client.post(f'{PREFIX}/respond', json=request_body(), headers={'Origin': 'http://testserver'}).status_code == 200
    assert len(dispatch) == 1


@pytest.mark.parametrize('profile', [None, '', 'other', '../default', 1])
def test_explicit_mounted_profile_equality_before_core(client, dispatch, profile):
    body = request_body()
    body['profile'] = profile
    response = client.post(f'{PREFIX}/respond', json=body)
    assert response.status_code == 403
    assert not dispatch


def test_named_mount_is_not_silently_body_selected(client, dispatch, monkeypatch):
    monkeypatch.setattr(api, 'profile_id', lambda: 'named')
    assert client.post(f'{PREFIX}/respond', json=request_body()).status_code == 403
    body = request_body()
    body['profile'] = 'named'
    assert client.post(f'{PREFIX}/respond', json=body).status_code == 200
    assert dispatch[0][1] == body


@pytest.mark.parametrize('failure,status', [(ValueError(SENTINEL), 400), (TypeError(SENTINEL), 400),
                                          (PermissionError(SENTINEL), 403), (RuntimeError(SENTINEL), 503),
                                          (OSError(SENTINEL), 503)])
def test_core_errors_are_generic_and_never_echo_values(client, dispatch, monkeypatch, caplog, failure, status):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(sys.modules['tui_gateway.methods_human_input'], 'call', fail)
    response = client.post(f'{PREFIX}/respond', json=request_body())
    assert response.status_code == status
    assert response.headers['cache-control'] == 'no-store'
    assert SENTINEL not in response.text and SENTINEL not in caplog.text
    assert set(response.json()) == {'error'}


@pytest.mark.parametrize('mode', ['no-server', 'no-methods', 'host', 'compute-child'])
def test_missing_or_foreign_process_dispatch_fails_closed(client, dispatch, monkeypatch, mode):
    with monkeypatch.context() as patch:
        if mode == 'no-server':
            patch.delitem(sys.modules, 'tui_gateway.server')
        elif mode == 'no-methods':
            patch.delitem(sys.modules, 'tui_gateway.methods_human_input')
        elif mode == 'host':
            patch.setenv('HERMES_PLUGIN_HOST_PROCESS', '1')
        else:
            patch.setenv('HERMES_COMPUTE_HOST_CHILD', '1')
        response = client.post(f'{PREFIX}/pending', json=request_body())
        assert response.status_code == 503
        assert response.json() == {'error': 'Human input unavailable'}
        assert not dispatch
        if mode == 'no-server':
            assert 'tui_gateway.server' not in sys.modules
        elif mode == 'no-methods':
            assert 'tui_gateway.methods_human_input' not in sys.modules


@pytest.mark.parametrize('content', ['[]', 'null', '{"result":NaN}', '{"result":'])
def test_invalid_json_never_reaches_core(client, dispatch, content):
    assert client.post(f'{PREFIX}/respond', content=content).status_code == 400
    assert not dispatch


def test_size_bound_and_unknown_route(client, dispatch, monkeypatch):
    monkeypatch.setattr(api, 'MAX_BODY', 128)
    assert client.post(f'{PREFIX}/respond', json=request_body()).status_code == 413
    assert client.post(f'{PREFIX}/arbitrary', json={}).status_code == 404
    assert not dispatch


def test_pydantic_and_unexpected_failures_do_not_escape_secure_boundary(client, dispatch, monkeypatch, caplog):
    from pydantic import BaseModel, ValidationError

    class Scope(BaseModel):
        revision: int

    with pytest.raises(ValidationError) as caught:
        Scope.model_validate({'revision': SENTINEL})
    for failure in (caught.value, KeyError(SENTINEL), HTTPException(400, SENTINEL)):
        def fail(*args, **kwargs):
            raise failure
        monkeypatch.setattr(sys.modules['tui_gateway.methods_human_input'], 'call', fail)
        response = client.post(f'{PREFIX}/respond', json=request_body())
        assert response.status_code in (400, 503)
        assert SENTINEL not in response.text and SENTINEL not in caplog.text
        assert response.headers['cache-control'] == 'no-store'


def test_unverified_mount_and_unusable_core_response_fail_closed(client, dispatch, monkeypatch):
    def unverified():
        raise ValueError(SENTINEL)
    with monkeypatch.context() as patch:
        patch.setattr(api, 'profile_id', unverified)
        response = client.post(f'{PREFIX}/pending', json=request_body())
        assert response.status_code == 403 and SENTINEL not in response.text
        assert not dispatch
    monkeypatch.setattr(sys.modules['tui_gateway.methods_human_input'], 'call', lambda *a, **kw: None)
    assert client.post(f'{PREFIX}/pending', json=request_body()).status_code == 503


def test_adapter_refuses_unlisted_operations_without_dispatch(dispatch):
    from plugins.cloudseed_mobile import human_input_adapter
    with pytest.raises(ValueError):
        human_input_adapter.call('request.answer', request_body(), principal='fixture-owner')
    assert not dispatch
