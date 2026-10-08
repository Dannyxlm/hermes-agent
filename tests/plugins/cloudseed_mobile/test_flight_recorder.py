"""Flight recorder contracts on synthetic data; never opens production stores."""
import base64
import datetime as dt
import json
import os
import stat
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from plugins.cloudseed_mobile import flight_recorder as fr
from plugins.cloudseed_mobile.dashboard import plugin_api as api

P = '/api/plugins/cloudseed_mobile/flight-recorder'
INSTALL = str(uuid.UUID('7a1c0de5-0000-4000-8000-00000000abcd'))
OTHER = str(uuid.UUID(int=8))
T = 1_791_400_000_000
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 64 + b'\xff\xd9'
SENTINEL = 'Shannon pricing hi'


@pytest.fixture
def client(monkeypatch, tmp_path):
    home = tmp_path
    monkeypatch.setenv('HERMES_HOME', str(home))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: home)
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'owner', 'owner_provider': 'basic'})
    fr._last_prune.clear()
    app = FastAPI()

    @app.middleware('http')
    async def session(request, call_next):
        if request.headers.get('authorization') == 'Bearer fixture':
            request.state.session = SimpleNamespace(user_id='owner', provider='basic')
        return await call_next(request)
    from hermes_cli.web_server_dashboard import _plugin_route_secret_scope
    app.include_router(api.router, prefix='/api/plugins/cloudseed_mobile', dependencies=[Depends(_plugin_route_secret_scope)])
    with TestClient(app) as c:
        c.headers['Authorization'] = 'Bearer fixture'
        yield c, home / 'mobile' / 'flight-recorder'


def events():
    return [
        {'kind': 'lifecycle', 't': T, 'state': 'launch'},
        {'kind': 'screen', 't': T + 1, 'screen': 'chat', 'session_id': '20261007_195052_eff4cd'},
        {'kind': 'tap', 't': T + 2, 'control': 'composer.send', 'screen': 'chat'},
        {'kind': 'rpc', 't': T + 3, 'method': 'prompt.submit', 'outcome': 'ok', 'ms': 41.5, 'bytes': 220,
         'session_id': '20261007_195052_eff4cd'},
        {'kind': 'http', 't': T + 4, 'route': '/api/plugins/cloudseed_mobile/deliverables', 'verb': 'GET',
         'outcome': 'error', 'ms': 900, 'status': 503},
        {'kind': 'error', 't': T + 5, 'domain': 'NSURLErrorDomain', 'code': -1001, 'screen': 'inbox'},
        {'kind': 'span', 't': T + 6, 'name': 'ChatOpenContent', 'outcome': 'ok', 'ms': 180},
        {'kind': 'connection', 't': T + 7, 'state': 'disconnected', 'transport': 'ws', 'code': 1006},
        {'kind': 'metrickit', 't': T + 8, 'payload_type': 'diagnostic', 'payload': {'hangDiagnostics': []}},
    ]


def batch(seq=0, install=INSTALL, evs=None, **extra):
    return {'profile': 'default', 'schema': 1, 'install_id': install, 'batch_seq': seq, 'app_build': '2026100705',
            'os_version': '27.0', 'device_class': 'iPhone18,1', 'events': evs if evs is not None else events(), **extra}


def flag(flag_id=None, install=INSTALL, shot=JPEG, **extra):
    body = {'profile': 'default', 'schema': 1, 'install_id': install, 'flag_id': flag_id or str(uuid.uuid4()),
            't': T, 'screen': 'chat', 'note': 'the composer jumped', 'app_build': '2026100705', 'os_version': '27.0',
            'device_class': 'iPhone18,1', 'events': events()[:3], **extra}
    if shot is not None:
        body['screenshot_jpeg_b64'] = base64.b64encode(shot).decode()
    return body


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_batch_is_stored_privately_with_receipt(client):
    c, root = client
    response = c.post(P + '/batch', json=batch())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['accepted'] == 9 and body['duplicate'] is False and len(body['receipt_id']) == 24
    files = list((root / 'events').iterdir())
    assert len(files) == 1 and files[0].name.endswith('.jsonl')
    record = json.loads(files[0].read_text())
    assert record['install_id'] == INSTALL and record['profile'] == 'default' and len(record['events']) == 9
    assert mode(files[0]) == 0o600 and mode(root / 'index.sqlite3') == 0o600
    assert mode(root) == mode(root / 'events') == mode(root.parent) == 0o700


def test_duplicate_batch_is_idempotent_and_reuse_conflicts(client):
    c, root = client
    first = c.post(P + '/batch', json=batch()).json()
    again = c.post(P + '/batch', json=batch()).json()
    assert again == {**first, 'accepted': 0, 'duplicate': True}
    assert c.post(P + '/batch', json=batch(evs=events()[:2])).status_code == 409
    [day] = (root / 'events').iterdir()
    assert len(day.read_text().splitlines()) == 1


@pytest.mark.parametrize('index,key', [
    (1, 'screen'), (2, 'control'), (3, 'method'), (3, 'session_id'), (4, 'route'), (5, 'domain'), (6, 'name'),
])
def test_free_text_cannot_ride_in_any_event_field(client, index, key):
    c, root = client
    evs = events()
    evs[index][key] = SENTINEL
    assert c.post(P + '/batch', json=batch(evs=evs)).status_code == 400
    assert not (root / 'events').exists() or not any((root / 'events').iterdir())


@pytest.mark.parametrize('mutate', [
    lambda e: e[0].update(note='hello'),                       # unknown key
    lambda e: e[0].update(kind='keystroke'),                   # unknown kind
    lambda e: e[3].update(ms=True),                            # bool is not a number
    lambda e: e[3].update(ms=-1),
    lambda e: e[3].update(outcome='weird'),
    lambda e: e[4].update(route='/api/x?q=secret'),            # query strings never allowed
    lambda e: e[0].pop('t'),
    lambda e: e[8].update(payload={'blob': 'x' * (fr.MAX_METRICKIT + 1)}),
])
def test_invalid_events_are_rejected(client, mutate):
    c, _ = client
    evs = events()
    mutate(evs)
    assert c.post(P + '/batch', json=batch(evs=evs)).status_code == 400


def test_envelope_bounds(client):
    c, _ = client
    assert c.post(P + '/batch', json=batch(extra_field=1)).status_code == 400
    assert c.post(P + '/batch', json=batch(evs=[])).status_code == 400
    assert c.post(P + '/batch', json=batch(evs=events()[:1] * (fr.MAX_BATCH_EVENTS + 1))).status_code in (400, 413)
    assert c.post(P + '/batch', json=batch(install='not-a-uuid')).status_code == 400
    assert c.post(P + '/batch', json=batch(install='{' + INSTALL + '}')).status_code == 400
    assert c.post(P + '/batch', json=batch(install=INSTALL.replace('-', ''))).status_code == 400
    # Swift's uuidString is upper-case: accept it, normalise, and dedupe across case.
    assert c.post(P + '/batch', json=batch(install=INSTALL.upper())).json()['duplicate'] is False
    assert c.post(P + '/batch', json=batch()).json()['duplicate'] is True
    assert c.post(P + '/batch', json={**batch(), 'schema': 2}).status_code == 400


def test_owner_and_profile_are_required(client, monkeypatch):
    c, _ = client
    assert c.post(P + '/batch', json={**batch(), 'profile': 'b'}).status_code == 403
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'another', 'owner_provider': 'basic'})
    assert c.post(P + '/batch', json=batch()).status_code == 403
    c.headers.pop('Authorization')
    assert c.post(P + '/batch', json=batch()).status_code == 401
    assert c.post(P + '/flag', json=flag()).status_code == 401
    assert c.post(P + '/delete', json={'profile': 'default', 'install_id': INSTALL}).status_code == 401


def test_flag_stores_screenshot_and_note_privately(client):
    c, root = client
    flag_id = str(uuid.uuid4())
    response = c.post(P + '/flag', json=flag(flag_id))
    assert response.status_code == 200, response.text
    [folder] = (root / 'flags').glob(f'*/{flag_id}')
    assert (folder / 'screenshot.jpg').read_bytes() == JPEG
    record = json.loads((folder / 'flag.json').read_text())
    assert record['note'] == 'the composer jumped' and record['has_screenshot'] is True
    assert 'screenshot_jpeg_b64' not in record
    assert mode(folder) == 0o700 and mode(folder / 'flag.json') == mode(folder / 'screenshot.jpg') == 0o600
    assert c.post(P + '/flag', json=flag(flag_id)).json()['duplicate'] is True
    assert c.post(P + '/flag', json=flag(flag_id, note='changed')).status_code == 409


def test_flag_without_screenshot_and_bad_images(client):
    c, root = client
    assert c.post(P + '/flag', json=flag(shot=None)).status_code == 200
    assert c.post(P + '/flag', json=flag(shot=b'\x89PNG\r\n\x1a\n')).status_code == 400
    assert c.post(P + '/flag', json=flag(shot=None, screenshot_jpeg_b64='***')).status_code == 400
    assert c.post(P + '/flag', json=flag(note='x' * (fr.MAX_NOTE + 1))).status_code == 400


def test_delete_removes_only_that_installation(client):
    c, root = client
    assert c.post(P + '/batch', json=batch(0)).status_code == 200
    assert c.post(P + '/batch', json=batch(0, install=OTHER)).status_code == 200
    assert c.post(P + '/flag', json=flag()).status_code == 200
    kept_flag = str(uuid.uuid4())
    assert c.post(P + '/flag', json=flag(kept_flag, install=OTHER)).status_code == 200
    response = c.post(P + '/delete', json={'profile': 'default', 'install_id': INSTALL})
    assert response.json() == {'ok': True, 'removed_batches': 1, 'removed_flags': 1}
    [day] = (root / 'events').iterdir()
    assert [json.loads(line)['install_id'] for line in day.read_text().splitlines()] == [OTHER]
    assert mode(day) == 0o600
    assert [p.name for p in (root / 'flags').glob('*/*')] == [kept_flag]
    # The same sequence can be uploaded again after a delete.
    assert c.post(P + '/batch', json=batch(0)).json()['duplicate'] is False
    assert c.post(P + '/delete', json={'profile': 'default', 'install_id': INSTALL, 'x': 1}).status_code == 400


def test_storage_cap_returns_507(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(fr, 'MAX_DAY_FILE', 100)
    response = c.post(P + '/batch', json=batch())
    assert response.status_code == 507 and response.json()['code'] == 'storage_full'


def test_symlinked_store_is_refused(client, tmp_path_factory):
    c, root = client
    elsewhere = tmp_path_factory.mktemp('elsewhere')
    root.parent.mkdir(mode=0o700)
    root.symlink_to(elsewhere, target_is_directory=True)
    assert c.post(P + '/batch', json=batch()).status_code == 400
    assert list(elsewhere.iterdir()) == []


def test_prune_removes_only_old_dated_entries(tmp_path):
    now = dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.timezone.utc)
    store = fr.FlightRecorderStore(tmp_path / 'mobile' / 'flight-recorder', now=lambda: now)
    store.put_batch('default', fr.validate_batch(batch()))
    events_dir, flags_dir = store.root / 'events', store.root / 'flags'
    (events_dir / '2026-08-01.jsonl').write_text('{}\n')
    (events_dir / 'notes.txt').write_text('keep')
    (flags_dir / '2026-08-01' / 'x').mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious').write_text('keep')
    (flags_dir / '2026-08-02').symlink_to(outside, target_is_directory=True)
    assert store.prune() == 2
    assert sorted(p.name for p in events_dir.iterdir()) == ['2026-10-07.jsonl', 'notes.txt']
    assert (outside / 'precious').exists()
