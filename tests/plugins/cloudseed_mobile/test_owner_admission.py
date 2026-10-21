"""Owner admission through the mounted router and real, isolated config files."""
import json
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import hermes_constants
from hermes_cli import config, config_backups
from hermes_cli.web_server_dashboard import _plugin_route_secret_scope
from plugins.cloudseed_mobile.dashboard import plugin_api as api

PROFILES = ('default', 'atlas', 'iris', 'matisse', 'nuru', 'vox')
PREFIX = '/api/plugins/cloudseed_mobile'
OWNER = {'owner_user_id': 'owner', 'owner_provider': 'basic'}


def batch(profile):
    return {
        'schema': 1, 'profile': profile,
        'install_id': '7a1c0de5-0000-4000-8000-00000000abcd', 'batch_seq': 1972,
        'app_build': '2026101315', 'os_version': '27.0', 'device_class': 'iPhone18,4',
        'events': [
            {'kind': 'lifecycle', 't': 1791400000000, 'state': 'launch'},
            {'kind': 'http', 't': 1791400000001, 'verb': 'GET', 'outcome': 'error',
             'route': PREFIX + '/deliverables', 'status': 503, 'ms': 900},
            {'kind': 'metrickit', 't': 1791400000002, 'payload_type': 'diagnostic',
             'payload': {'hangDiagnostics': []}},
        ],
    }


@pytest.fixture
def client(monkeypatch, tmp_path):
    home = tmp_path / 'installation'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: home)
    monkeypatch.setattr(config, 'ensure_hermes_home', lambda: None)
    monkeypatch.setattr(config_backups, 'backup_config', lambda *a, **k: None)
    (home / 'config.yaml').write_text(json.dumps({'cloudseed_mobile': OWNER}))
    for profile in PROFILES[1:]:
        named = home / 'profiles' / profile
        named.mkdir(parents=True)
        (named / 'config.yaml').write_text('{}')
    app = FastAPI()

    @app.middleware('http')
    async def verified_session(request, call_next):
        identity = request.headers.get('x-test-session', 'owner')
        if identity != 'missing':
            request.state.session = SimpleNamespace(
                user_id='foreign' if identity == 'foreign-user' else 'owner',
                provider='foreign' if identity == 'foreign-provider' else 'basic',
            )
        return await call_next(request)

    app.include_router(api.router, prefix=PREFIX, dependencies=[Depends(_plugin_route_secret_scope)])
    with TestClient(app) as test_client:
        yield test_client, home


@pytest.mark.parametrize('profile', PROFILES)
def test_installation_owner_can_upload_to_each_profile(client, profile):
    c, home = client
    url = PREFIX + '/flight-recorder/batch'
    headers = {'Authorization': 'Bearer fixture'}
    response = c.post(url, params={'profile': profile}, json=batch(profile), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == 3
    assert response.json()['duplicate'] is False
    duplicate = c.post(url, params={'profile': profile}, json=batch(profile), headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()['duplicate'] is True
    [day] = (home / 'mobile' / 'flight-recorder' / 'events').glob('*.jsonl')
    [record] = [json.loads(line) for line in day.read_text().splitlines()]
    assert record['profile'] == profile
    assert record['batch_seq'] == 1972
    cookie = c.post(url, params={'profile': profile}, json=batch(profile), headers={'Origin': 'http://testserver'})
    assert cookie.status_code == 200


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('case,status', [
    ('foreign-user', 403), ('foreign-provider', 403), ('missing', 401),
    ('cross-origin', 403), ('no-origin', 403), ('profile-mismatch', 403),
])
def test_owner_admission_preserves_request_boundaries(client, profile, case, status):
    c, home = client
    body = batch(profile)
    headers = {'Authorization': 'Bearer fixture'}
    if case in ('foreign-user', 'foreign-provider', 'missing'):
        headers['x-test-session'] = case
    elif case in ('cross-origin', 'no-origin'):
        headers = {'Origin': 'https://foreign.invalid'} if case == 'cross-origin' else {}
    else:
        body['profile'] = 'atlas' if profile == 'default' else 'default'
    response = c.post(PREFIX + '/flight-recorder/batch', params={'profile': profile}, json=body, headers=headers)
    assert response.status_code == status
    assert not (home / 'mobile' / 'flight-recorder' / 'events').exists()


@pytest.mark.parametrize('profile', PROFILES[1:])
def test_named_profile_cannot_redefine_installation_owner(client, profile):
    c, home = client
    (home / 'profiles' / profile / 'config.yaml').write_text(json.dumps({
        'cloudseed_mobile': {'owner_user_id': 'foreign', 'owner_provider': 'basic'},
    }))
    headers = {'Authorization': 'Bearer fixture'}
    response = c.post(PREFIX + '/flight-recorder/batch', params={'profile': profile}, json=batch(profile), headers=headers)
    assert response.status_code == 200, response.text
    headers['x-test-session'] = 'foreign-user'
    assert c.post(PREFIX + '/flight-recorder/batch', params={'profile': profile}, json=batch(profile), headers=headers).status_code == 403
    (home / 'config.yaml').write_text('{}')
    assert c.post(PREFIX + '/flight-recorder/batch', params={'profile': profile}, json=batch(profile), headers=headers).status_code == 403
