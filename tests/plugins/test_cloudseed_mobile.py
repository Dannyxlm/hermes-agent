"""Synthetic mobile contracts; never open the production integration stores."""
import base64
import tempfile
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.cloudseed_mobile.dashboard import plugin_api as api


@pytest.fixture
def client(monkeypatch, tmp_path):
    # --basetemp places fixtures in scratch; shared home guards remain enabled.
    with __import__('contextlib').nullcontext(str(tmp_path)) as d:
        home = Path(d)
        monkeypatch.setenv('HERMES_HOME', d)
        import hermes_constants
        monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: home)
        monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'owner', 'owner_provider': 'basic'})
        app = FastAPI()
        @app.middleware('http')
        async def session(request, call_next):
            if request.headers.get('authorization') == 'Bearer fixture':
                request.state.session = SimpleNamespace(user_id='owner', provider='basic')
            return await call_next(request)
        app.include_router(api.router, prefix='/api/plugins/cloudseed_mobile')
        with TestClient(app) as c:
            c.headers['Authorization'] = 'Bearer fixture'
            yield c, home


P = '/api/plugins/cloudseed_mobile'
DEVICE = str(uuid.UUID(int=1))
SECRET = 'a' * 64

def post(c, domain, op, **extra):
    return c.post(f'{P}/{domain}/{op}', json={'profile': 'default', 'device_id': DEVICE, 'secret': SECRET, **extra})


def test_catalog_cycle_search_tombstone(client, monkeypatch):
    c, home = client
    monkeypatch.setattr(api, 'photo_store', lambda: api.PhotoCatalog(home / 'webui/photo-catalog/catalog.sqlite3', free_bytes=lambda: 10 * 1024**3))
    assert post(c, 'photo-catalog', 'register').status_code == 200
    epoch = post(c, 'photo-catalog', 'begin', count=1).json()['epoch']
    item = {'id': 'b'*64, 'fingerprint': 'c'*64, 'created': 1, 'albums': ['Fixture'], 'text': 'synthetic cat', 'favorite': False, 'screenshot': False, 'preview': base64.b64encode(b'\xff\xd8\xff\xd9').decode()}
    assert post(c, 'photo-catalog', 'manifest', epoch=epoch, items=[item]).json()['missing'] == [item['id']]
    assert post(c, 'photo-catalog', 'put', epoch=epoch, item=item).status_code == 200
    assert post(c, 'photo-catalog', 'finish', epoch=epoch).json()['complete']
    assert len(post(c, 'photo-catalog', 'search', query={'text': 'cat'}).json()['items']) == 1
    assert post(c, 'photo-catalog', 'preview', asset_id=item['id']).json()['preview'] == item['preview']
    epoch2 = post(c, 'photo-catalog', 'begin', count=0).json()['epoch']
    assert post(c, 'photo-catalog', 'finish', epoch=epoch2).json()['count'] == 0
    assert post(c, 'photo-catalog', 'put', epoch=epoch, item=item).status_code == 400
    assert post(c, 'photo-catalog', 'disconnect').status_code == 200
    assert post(c, 'photo-catalog', 'register').status_code == 400


@pytest.mark.parametrize('domain', ['photo-catalog', 'iphone-reminders'])
def test_auth_unknown_and_explicit_rebind(client, domain):
    c, home = client
    c.headers.pop('authorization')
    assert post(c, domain, 'register').status_code == 401
    c.headers['authorization'] = 'Bearer fixture'
    assert post(c, domain, 'poll' if domain == 'iphone-reminders' else 'status').status_code == 400
    assert post(c, domain, 'register').status_code == 200
    store = api.reminder_store() if domain == 'iphone-reminders' else api.photo_store()
    with store.connection() as db:
        db.execute('UPDATE devices SET principal=? WHERE id=?', ('legacy', DEVICE))
    assert post(c, domain, 'register').status_code == 400
    assert post(c, domain, 'register', rebind=True, secret='f'*64).status_code == 400
    assert post(c, domain, 'register', rebind=True).status_code == 200


def test_reminder_idempotency_expiry_disconnect(client):
    c, home = client
    assert post(c, 'iphone-reminders', 'register').status_code == 200
    store = api.reminder_store()
    rid = str(uuid.uuid4())
    payload = {'title': 'synthetic'}
    store.enqueue(DEVICE, rid, 'create', payload, 'default')
    store.enqueue(DEVICE, rid, 'create', payload, 'default')
    assert post(c, 'iphone-reminders', 'poll').json()['command']['id'] == rid
    assert post(c, 'iphone-reminders', 'poll').json()['command']['id'] == rid
    assert post(c, 'iphone-reminders', 'ack', request_id=rid, state='completed', result={}).status_code == 200
    assert post(c, 'iphone-reminders', 'ack', request_id=rid, state='completed', result={}).status_code == 200
    assert post(c, 'iphone-reminders', 'ack', request_id=rid, state='failed', result={}).status_code == 400
    expired = str(uuid.uuid4())
    store.enqueue(DEVICE, expired, 'create', payload, 'default')
    with store.connection() as db:
        db.execute('UPDATE commands SET expires=0 WHERE id=?', (expired,))
    assert store.status(DEVICE, expired, 'default')['state'] == 'expired'
    assert post(c, 'iphone-reminders', 'poll').json()['command'] is None
    processing = str(uuid.uuid4())
    store.enqueue(DEVICE, processing, 'create', payload, 'default')
    assert post(c, 'iphone-reminders', 'poll').json()['command']['id'] == processing
    assert post(c, 'iphone-reminders', 'disconnect').status_code == 200
    with store.connection() as db:
        assert db.execute('SELECT state FROM commands WHERE id=?', (processing,)).fetchone()[0] == 'unconfirmed'
    assert post(c, 'iphone-reminders', 'register', rebind=True).status_code == 400


def test_memory_closed_names_size_and_symlinks(client):
    c, home = client
    assert c.post(P+'/memory/write', json={'section': 'memory', 'content': 'synthetic'}).status_code == 200
    assert c.get(P+'/memory').json()['memory'] == 'synthetic'
    assert c.post(P+'/memory/write', json={'section': '../auth.json', 'content': ''}).status_code == 400
    assert c.post(P+'/memory/write', json={'section': 'soul', 'content': 'x' * (api.MAX_MEMORY + 1)}).status_code == 413
    (home/'SOUL.md').symlink_to(home/'memories/MEMORY.md')
    assert c.get(P+'/memory').status_code == 400
    assert c.post(P+'/memory/write', json={'section': 'soul', 'content': ''}).status_code == 400


def test_account_primary_cas_uses_native_store(client):
    import json
    c, home = client
    store = {'credential_pool': {'openai-codex': [
        {'id': 'first', 'label': 'synthetic-private-label', 'source': 'manual', 'priority': 0, 'auth_type': 'oauth', 'access_token': 'fixture-only-not-a-token'},
        {'id': 'second', 'label': 'synthetic@example.invalid', 'source': 'manual', 'priority': 1, 'auth_type': 'oauth', 'access_token': 'fixture-only-other'},
    ]}}
    path = home/'auth.json'
    path.write_text(json.dumps(store))
    original = path.read_bytes()
    response = c.get(P+'/provider/accounts')
    assert response.status_code == 200
    assert path.read_bytes() == original  # inventory is strictly read-only
    assert 'fixture-only' not in response.text
    assert 'synthetic-private-label' not in response.text
    section = next(p for p in response.json()['providers'] if p['id'] == 'openai-codex')
    body = {'provider': 'openai-codex', 'account_id': 'second', 'profile_id': 'default', 'revision': section['revision']}
    changed = c.post(P+'/provider/accounts/primary', json=body)
    assert changed.status_code == 200, changed.text
    section2 = next(p for p in changed.json()['providers'] if p['id'] == 'openai-codex')
    assert section2['primary_id'] == 'second'
    assert section2['revision'] != section['revision']
    assert c.post(P+'/provider/accounts/primary', json=body).status_code == 409
    saved = json.loads(path.read_text())
    assert next(r for r in saved['credential_pool']['openai-codex'] if r['id'] == 'second')['priority'] == 0


def test_owner_profile_payload_boundaries(client, monkeypatch):
    c, home = client
    assert post(c, 'photo-catalog', 'register', profile='other').status_code == 403
    monkeypatch.setattr(api, 'owner_config', lambda: {})
    assert c.get(P+'/memory').status_code == 403
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'other', 'owner_provider': 'basic'})
    assert c.get(P+'/memory').status_code == 403
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'owner', 'owner_provider': 'basic'})
    assert c.post(P+'/memory/write', content=b'x'*(api.MAX_BODY+1)).status_code == 413
    assert c.post(P+'/memory/write', content='{"section":"soul","content":NaN}').status_code == 400


def test_memory_directory_symlink_refused(client):
    c, home = client
    elsewhere = home/'other'
    elsewhere.mkdir()
    (home/'memories').symlink_to(elsewhere, target_is_directory=True)
    assert c.post(P+'/memory/write', json={'section': 'memory', 'content': 'synthetic'}).status_code == 400
    assert not (elsewhere/'MEMORY.md').exists()


def test_catalog_limits_low_space_and_cursor(client, monkeypatch):
    c, home = client
    assert post(c, 'photo-catalog', 'register').status_code == 200
    assert post(c, 'photo-catalog', 'manifest', epoch='old', items=[{}]*501).status_code == 400
    epoch = post(c, 'photo-catalog', 'begin', count=1).json()['epoch']
    item = {'id': 'b'*64, 'fingerprint': 'c'*64, 'created': 1, 'albums': [], 'text': 'synthetic', 'favorite': False, 'screenshot': False, 'preview': base64.b64encode(b'\xff\xd8\xff\xd9').decode()}
    assert post(c, 'photo-catalog', 'manifest', epoch=epoch, items=[item]).status_code == 200
    monkeypatch.setattr(api, 'photo_store', lambda: api.PhotoCatalog(home/'webui/photo-catalog/catalog.sqlite3', free_bytes=lambda: 0))
    assert post(c, 'photo-catalog', 'put', epoch=epoch, item=item).status_code == 507
    oversized = {**item, 'preview': base64.b64encode(b'\xff\xd8\xff'+b'x'*262144+b'\xff\xd9').decode()}
    assert post(c, 'photo-catalog', 'put', epoch=epoch, item=oversized).status_code == 400
