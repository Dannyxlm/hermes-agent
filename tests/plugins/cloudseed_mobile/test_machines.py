"""Owner-only fleet reads through the native plugin mount, with synthetic state."""
import builtins
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from plugins.cloudseed_mobile.dashboard import plugin_api as api


pytestmark = pytest.mark.platforms('posix')
P = '/api/plugins/cloudseed_mobile/machines'
FIXTURE = Path(__file__).parent / 'fixtures/machines-v1.json'
CONTRACT = json.loads(FIXTURE.read_bytes())
HASH = 'a8e4d8ec894d4a660254897f3745853c7b803854357caede2086943998929cdc'


@pytest.fixture
def client(monkeypatch, tmp_path):
    import hermes_constants
    from hermes_cli.web_server_dashboard import _plugin_route_secret_scope

    home = tmp_path / 'hermes'
    home.mkdir()
    user = tmp_path / 'os-user'
    fleet = user / '.local/state/cloudseed/fleet/latest.json'
    fleet.parent.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(tmp_path / 'profile-home'))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'profile-home')
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: home)
    monkeypatch.setattr(pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir=str(user)))
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'owner', 'owner_provider': 'basic'})
    app = FastAPI()

    @app.middleware('http')
    async def session(request, call_next):
        identities = {
            'Bearer fixture': ('owner', 'basic'),
            'Bearer wrong-owner': ('other', 'basic'),
            'Bearer wrong-provider': ('owner', 'other'),
        }
        identity = identities.get(request.headers.get('authorization'))
        if identity:
            request.state.session = SimpleNamespace(user_id=identity[0], provider=identity[1])
        return await call_next(request)

    app.include_router(api.router, prefix='/api/plugins/cloudseed_mobile',
                       dependencies=[Depends(_plugin_route_secret_scope)])
    with TestClient(app) as c:
        c.headers['Authorization'] = 'Bearer fixture'
        yield c, fleet


def assert_unavailable(response):
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.json() == CONTRACT['route']['supported_unavailable']


def test_frozen_contract_hash():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == HASH


@pytest.mark.parametrize('case', CONTRACT['cases'].values(), ids=CONTRACT['cases'])
def test_contract_machine_objects_and_sample_time_pass_through(client, case):
    c, fleet = client
    fleet.write_text(json.dumps(case['file']))
    response = c.get(P)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.json() == {
        'ok': True, 'status': 'ok', 'protocol_version': 1,
        'feature_versions': {'machines': 1},
        'generated_at': case['file']['generated_at'], 'machines': case['file']['machines'],
    }


@pytest.mark.parametrize('authorization,status', [
    (None, 401), ('Bearer wrong-owner', 403), ('Bearer wrong-provider', 403),
    ('Bearer service-token', 401),
])
def test_owner_authentication_before_read(client, monkeypatch, authorization, status):
    c, fleet = client
    c.headers.pop('authorization')
    if authorization:
        c.headers['Authorization'] = authorization
    real_open = os.open

    def no_fleet_read(path, flags, *args, **kwargs):
        assert Path(path) != fleet, 'Unauthorized request must not read fleet state'
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', no_fleet_read)
    assert c.get(P).status_code == status


def test_missing_state_is_supported_unavailable_without_creating_it(client):
    c, fleet = client
    assert_unavailable(c.get(P))
    assert not fleet.exists()


@pytest.mark.parametrize('raw', [
    b'not json', b'\xff', b'[]', b'null', b'{}',
    b'{"generated_at": "2026-10-26T18:01:05Z", "machines": []}',
    *[json.dumps({**CONTRACT['cases']['all_fresh']['file'], **override}).encode()
      for override in [
          {'protocol_version': 2}, {'protocol_version': True},
          {'feature_versions': {'machines': 2}}, {'feature_versions': None},
          {'generated_at': None}, {'generated_at': 'not-a-time'},
          {'machines': None}, {'machines': {}}, {'machines': [None]},
      ]],
    json.dumps(CONTRACT['cases']['all_fresh']['file']).replace('23.5', 'NaN').encode(),
    json.dumps(CONTRACT['cases']['all_fresh']['file']).replace('23.5', '1e999').encode(),
    b'[' * 1200 + b']' * 1200,
], ids=[f'invalid-{i}' for i in range(18)])
def test_malformed_state_is_supported_unavailable(client, raw):
    c, fleet = client
    fleet.write_bytes(raw)
    assert_unavailable(c.get(P))


@pytest.mark.parametrize('extra_bytes,available', [(0, True), (1, False)])
def test_256_kib_file_boundary(client, extra_bytes, available):
    c, fleet = client
    raw = json.dumps(CONTRACT['cases']['all_fresh']['file']).encode()
    fleet.write_bytes(raw + b' ' * (256 * 1024 - len(raw) + extra_bytes))
    response = c.get(P)
    if available:
        assert response.status_code == 200
        assert response.json()['machines'] == CONTRACT['cases']['all_fresh']['file']['machines']
    else:
        assert_unavailable(response)


def test_unreadable_file_is_sanitized(client, monkeypatch):
    c, fleet = client
    real_open = os.open

    def denied(path, flags, *args, **kwargs):
        if Path(path) == fleet:
            raise PermissionError('private-path-and-error-must-not-leak')
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', denied)
    assert_unavailable(c.get(P))


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'fifo'])
def test_nonregular_state_is_unavailable(client, kind):
    c, fleet = client
    if kind == 'directory':
        fleet.mkdir()
    elif kind == 'fifo':
        os.mkfifo(fleet)
    else:
        target = fleet.with_name('elsewhere.json')
        target.write_text(json.dumps(CONTRACT['cases']['all_fresh']['file']))
        fleet.symlink_to(target)
    assert_unavailable(c.get(P))


def test_unknown_fields_survive_but_process_fields_never_leave_route(client):
    c, fleet = client
    sample = copy.deepcopy(CONTRACT['cases']['all_fresh']['file'])
    sample['top'] = 'private-envelope-process'
    machine = sample['machines'][0]
    machine['future'] = {'opaque': [None, {'value': 42}]}
    expected = copy.deepcopy(sample['machines'])
    machine['top'] = 'private-top'
    machine['cpu']['process'] = 'private-process'
    machine['future']['opaque'].append({'processes': ['private-nested'], 'safe': True})
    expected[0]['future']['opaque'].append({'safe': True})
    fleet.write_text(json.dumps(sample))
    response = c.get(P)
    assert response.status_code == 200
    assert response.json()['machines'] == expected
    assert 'private-' not in response.text


def test_read_has_no_writes_or_processes_and_ignores_path_query(client, monkeypatch):
    c, fleet = client
    sample = CONTRACT['cases']['all_fresh']['file']
    fleet.write_text(json.dumps(sample))
    before = {p: p.read_bytes() for p in fleet.parents[4].rglob('*') if p.is_file()}
    forbidden = Mock(side_effect=AssertionError('Fleet reads cannot write or launch processes'))
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    real_open = os.open

    def read_only_fd(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return real_open(path, flags, *args, **kwargs)

    def read_only(original):
        def opened(path, mode='r', *args, **kwargs):
            assert not any(flag in mode for flag in 'wax+')
            return original(path, mode, *args, **kwargs)
        return opened

    with monkeypatch.context() as guard:
        guard.setattr(os, 'open', read_only_fd)
        guard.setattr(builtins, 'open', read_only(builtins.open))
        guard.setattr(io, 'open', read_only(io.open))
        response = c.get(P, params={'path': '/not-an-approved-file', 'file': '/also-forbidden'})
    assert response.status_code == 200
    assert response.json()['machines'] == sample['machines']
    assert {p: p.read_bytes() for p in fleet.parents[4].rglob('*') if p.is_file()} == before
    forbidden.assert_not_called()


def test_named_profile_cannot_redirect_the_fleet_file(client):
    c, fleet = client
    fleet.write_text(json.dumps(CONTRACT['cases']['all_fresh']['file']))
    home = Path(os.environ['HERMES_HOME'])
    profile = home / 'profiles/b'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('{}')
    response = c.get(P, params={'profile': 'b'})
    assert response.status_code == 200
    assert response.json()['machines'] == CONTRACT['cases']['all_fresh']['file']['machines']
    assert not (profile / '.local').exists()


def test_native_dashboard_mount_and_disabled_unsupported(client, monkeypatch):
    from hermes_cli import web_server_dashboard as dashboard

    root = Path(__file__).resolve().parents[3]
    monkeypatch.setattr(dashboard, '_dashboard_plugin_search_dirs', lambda: [(root / 'plugins', 'bundled')])
    entries = [entry for entry in dashboard._discover_dashboard_plugins() if entry['name'] == 'cloudseed_mobile']
    assert entries

    async def scope():
        yield

    monkeypatch.setattr(dashboard, '_plugin_route_secret_scope', scope)
    for disabled, expected in [(set(), 401), ({'cloudseed_mobile'}, 404)]:
        app = FastAPI()
        monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', SimpleNamespace(
            app=app, _get_dashboard_plugins=lambda *, force_rescan=False: entries))
        monkeypatch.setitem(sys.modules, 'hermes_cli.plugins_cmd', SimpleNamespace(
            _get_enabled_set=lambda: set(), _get_disabled_set=lambda: disabled))
        dashboard._mount_plugin_api_routes()
        with TestClient(app) as mounted:
            assert mounted.get(P).status_code == expected
