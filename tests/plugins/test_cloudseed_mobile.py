"""Synthetic mobile contracts; never open the production integration stores."""
import base64
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
    home = tmp_path
    monkeypatch.setenv('HERMES_HOME', str(home))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: home)
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'owner', 'owner_provider': 'basic'})
    app = FastAPI()
    @app.middleware('http')
    async def session(request, call_next):
        if request.headers.get('authorization') == 'Bearer fixture' or request.cookies.get('fixture_session') == 'owner':
            request.state.session = SimpleNamespace(user_id='owner', provider='basic')
        return await call_next(request)
    from hermes_cli.web_server_dashboard import _plugin_route_secret_scope
    from fastapi import Depends
    app.include_router(api.router, prefix='/api/plugins/cloudseed_mobile', dependencies=[Depends(_plugin_route_secret_scope)])
    with TestClient(app) as c:
        c.headers['Authorization'] = 'Bearer fixture'
        yield c, home


P = '/api/plugins/cloudseed_mobile'
DEVICE = str(uuid.UUID(int=1))
SECRET = 'a' * 64

def post(c, domain, op, **extra):
    return c.post(f'{P}/{domain}/{op}', json={'profile': 'default', 'device_id': DEVICE, 'secret': SECRET, **extra})


def test_session_activity_profile_scope_and_metadata_only(client):
    from tui_gateway.side_task_activity import registry
    c, home = client
    b = home/'profiles'/'b'
    b.mkdir(parents=True)
    (b/'config.yaml').write_text('{}')
    key = registry.register(home, 'stored', 'bg', 'background')
    registry.finish(key, 'completed')
    registry.register(b, 'stored', 'btw', 'btw')
    url = P+'/session-activity?profile=default&stored_session_id=stored'
    response = c.get(url)
    assert response.status_code == 200
    assert response.json() == {'version': 1, 'profile': 'default', 'stored_session_id': 'stored',
                               'epoch': registry.epoch, 'complete': True,
                               'tasks': registry.snapshot(home, ['stored'])}
    assert c.get(P+'/session-activity?profile=b&stored_session_id=stored').json()['tasks'][0]['task_id'] == 'btw'
    assert c.get(url).json()['tasks'][0]['task_id'] == 'bg'
    assert not (home/'state.db').exists() and not (b/'state.db').exists()
    assert c.get(P+'/session-activity?profile=missing&stored_session_id=stored').status_code == 404
    assert not (home/'profiles'/'missing').exists()
    assert c.get(P+'/session-activity?profile=default&stored_session_id=').status_code == 400
    c.headers.pop('Authorization')
    assert c.get(url).status_code == 401


def test_session_activity_fences_and_unavailable_store(client, monkeypatch):
    c, home = client
    url = P+'/session-activity?profile=default&stored_session_id=stored'
    assert c.get(P+'/session-activity?stored_session_id=stored').status_code == 422
    assert c.get(P+'/session-activity?profile=../escape&stored_session_id=stored').status_code == 400
    (home/'state.db').write_bytes(b'not a database')
    response = c.get(url)
    assert response.status_code == 503
    assert response.json() == {'error': 'Mobile storage unavailable'}
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'another', 'owner_provider': 'basic'})
    assert c.get(url).status_code == 403


def test_session_activity_observes_detached_worker_after_parent_retirement(client, monkeypatch):
    import threading
    from tui_gateway import server
    c, home = client
    entered, release, completed = (threading.Event() for _ in range(3))
    def body():
        entered.set()
        assert release.wait(10)
        return 'private result'
    monkeypatch.setattr(server, '_emit', lambda *a: completed.set())
    session = {'profile_home': str(home), 'session_key': 'stored', 'cwd': str(home)}
    server._sessions['activity-test'] = session
    url = P+'/session-activity?profile=default&stored_session_id=stored'
    try:
        result = server._spawn_side_agent('r', session, 'detached', 'activity-test', 'background.complete', body)
        assert 'result' in result
        assert entered.wait(10)
        server._sessions.pop('activity-test')
        session['session_key'] = 'rotated'
        assert c.get(url).json()['tasks'][0]['status'] == 'running'
        release.set()
        assert completed.wait(10)
        response = c.get(url)
        assert response.json()['tasks'][0]['status'] == 'completed'
        assert 'private result' not in response.text
    finally:
        release.set()
        server._sessions.pop('activity-test', None)
        for thread in threading.enumerate():
            if thread.name == 'side-agent-detached':
                thread.join(10)


def test_session_activity_compression_lineage_read_only(client):
    import sqlite3
    from tui_gateway.side_task_activity import registry
    c, home = client
    db_path = home/'state.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT)')
        db.executemany('INSERT INTO sessions VALUES(?,?,?)', [
            ('root', None, 'compression'), ('tip', 'root', None), ('branch', 'tip', None)])
    before = db_path.read_bytes()
    registry.register(home, 'root', 'bg', 'background')
    registry.register(home, 'tip', 'btw', 'btw')
    registry.register(home, 'branch', 'foreign', 'background')
    for sid in ['root', 'tip']:
        response = c.get(P+f'/session-activity?profile=default&stored_session_id={sid}')
        assert response.status_code == 200
        assert {t['task_id'] for t in response.json()['tasks']} == {'bg', 'btw'}
    assert db_path.read_bytes() == before


def test_catalog_cycle_search_tombstone(client, monkeypatch):
    c, home = client
    import asyncio
    def store():
        # Exercise actual SQLite while guarding the HTTP threadpool boundary.
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return api.PhotoCatalog(home / 'webui/photo-catalog/catalog.sqlite3', free_bytes=lambda: 10 * 1024**3)
    monkeypatch.setattr(api, 'photo_store', store)
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


@pytest.mark.parametrize('label', ['DANNY-ANT', 'SJ-ANT', 'SYB-Codex', 'Work Max', 'Personal (Max) 2._-', 'W ' * 19 + 'WW'],
                         ids=['danny-ant', 'sj-ant', 'syb-codex', 'spaces', 'alphabet', 'nickname-limit'])
def test_account_display_saved_key_name(label):
    from plugins.cloudseed_mobile.account_render import _safe_entry_label
    entry = SimpleNamespace(label=label, auth_type='oauth', access_token='')
    assert _safe_entry_label(entry, 3) == label


def _display_test_jwt(claims):
    import json
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'fixture.' + payload + '.fixture'


@pytest.mark.parametrize('label', [
    'sk-fictional', 'Bearer fixture', 'SK-fictional', 'bearer fixture',
    'a' * 20, 'a' * 19 + '/', 'a' * 19 + '=', 'x ' * 21,
    'Work\nMax', 'Work\x00Max', 'Work\x7fMax', 'Work\x85Max',
    ' ', '', None, 12, 'Work <Max>',
    'ada@example.com', 'synthetic@example.invalid', 'DANNY@ANT',
], ids=[f'case-{i}' for i in range(20)])
def test_account_display_rejects_unsafe_or_email_labels(label):
    from plugins.cloudseed_mobile.account_render import _safe_entry_label
    entry = SimpleNamespace(label=label, auth_type='oauth', access_token=_display_test_jwt({'email': 'ada@example.com'}),
                            last_error_message='Work Max')
    assert _safe_entry_label(entry, 4) == 'Account 4'


@pytest.mark.parametrize('extra', [{}, {'id_token': 'x'}], ids=['access-token', 'id-token'])
def test_account_display_never_reads_token_email(extra):
    from plugins.cloudseed_mobile.account_render import _safe_entry_label
    jwt = _display_test_jwt({'email': 'ada@example.com', 'https://api.openai.com/profile': {'email': 'ada@example.com'}})
    entry = SimpleNamespace(label='', auth_type='oauth', access_token=jwt,
                            extra={'id_token': jwt} if extra else None)
    assert _safe_entry_label(entry, 7) == 'Account 7'


def test_account_display_missing_label_is_generic():
    from plugins.cloudseed_mobile.account_render import _safe_entry_label
    entry = SimpleNamespace(label=None, auth_type='oauth', access_token=None)
    assert _safe_entry_label(entry, 7) == 'Account 7'


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
    accounts = response.json()['providers'][1]['accounts']
    assert [a['id'] for a in accounts] == ['first', 'second']
    assert [a['priority'] for a in accounts] == [0, 1]
    assert accounts[0]['is_primary'] and not accounts[1]['is_primary']
    assert accounts[0]['label'] == 'Account 1'  # token-like saved label rejected
    assert accounts[1]['label'] == 'Account 2'  # email-shaped label never shown
    assert 'synthetic@example.invalid' not in response.text
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
    b = home/'profiles'/'b'
    b.mkdir(parents=True)
    (b/'config.yaml').write_text('{}')
    borrowed = c.get(P+'/provider/accounts?profile=b')
    borrowed_section = next(p for p in borrowed.json()['providers'] if p['id'] == 'openai-codex')
    assert borrowed_section['scope'] == 'shared_root'
    assert not borrowed_section['can_set_primary']
    denied = c.post(P+'/provider/accounts/primary?profile=b', json={
        'provider': 'openai-codex', 'account_id': 'first', 'profile_id': 'b', 'revision': borrowed_section['revision']})
    assert denied.status_code == 403
    assert json.loads(path.read_text()) == saved
    assert not (b/'auth.json').exists()  # no borrowed credential copy


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


def test_owner_cookie_origin(client):
    c, _ = client
    c.headers.pop('authorization')
    c.cookies.set('fixture_session', 'owner')
    body = {'section': 'soul', 'content': 'fixture'}
    assert c.post(P+'/memory/write', json=body).status_code == 403
    assert c.post(P+'/memory/write', json=body, headers={'Origin': 'https://attacker.invalid'}).status_code == 403
    assert c.post(P+'/memory/write', json=body, headers={'Origin': 'http://testserver'}).status_code == 200
    assert c.get(P+'/memory').json()['soul'] == 'fixture'


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


def test_reminder_pre_pair_status_is_capability_only(client):
    c, _ = client
    response = c.post(P+'/iphone-reminders/status', json={'profile': 'default'})
    assert response.status_code == 200
    assert response.json() == {'ok': True, 'protocol_version': 1}
    assert post(c, 'iphone-reminders', 'status').status_code == 400


def test_catalog_pagination_epoch_fence_and_foreign_device(client, monkeypatch):
    c, home = client
    monkeypatch.setattr(api, 'photo_store', lambda: api.PhotoCatalog(home/'webui/photo-catalog/catalog.sqlite3', free_bytes=lambda: 10 * 1024**3))
    assert post(c, 'photo-catalog', 'register').status_code == 200
    epoch = post(c, 'photo-catalog', 'begin', count=2).json()['epoch']
    items = [{'id': digit*64, 'fingerprint': 'c'*64, 'created': 1, 'albums': [], 'text': 'fixture', 'favorite': False, 'screenshot': False, 'preview': base64.b64encode(b'\xff\xd8\xff\xd9').decode()} for digit in ['a', 'b']]
    assert post(c, 'photo-catalog', 'manifest', epoch=epoch, items=items).status_code == 200
    assert post(c, 'photo-catalog', 'manifest', epoch=epoch, items=items).status_code == 200
    for item in items:
        assert post(c, 'photo-catalog', 'put', epoch=epoch, item=item).status_code == 200
    assert post(c, 'photo-catalog', 'finish', epoch=epoch).json()['complete']
    page = post(c, 'photo-catalog', 'search', query={'limit': 1}).json()
    next_page = post(c, 'photo-catalog', 'search', query={'limit': 1, 'cursor': page['next_cursor']}).json()
    assert [page['items'][0]['id'], next_page['items'][0]['id']] == ['a'*64, 'b'*64]
    assert next_page['next_cursor'] is None
    assert post(c, 'photo-catalog', 'search', query={'limit': 2, 'cursor': page['next_cursor']}).status_code == 400
    with api.photo_store().connection() as db:
        db.execute('UPDATE devices SET principal=? WHERE id=?', ('foreign', DEVICE))
    for op, extra in [('status', {}), ('search', {}), ('preview', {'asset_id': 'a'*64})]:
        assert post(c, 'photo-catalog', op, **extra).status_code == 400
    assert post(c, 'photo-catalog', 'register', rebind=True).status_code == 200
    assert post(c, 'photo-catalog', 'begin', count=0).status_code == 200
    assert post(c, 'photo-catalog', 'search', query={'limit': 1, 'cursor': page['next_cursor']}).status_code == 400


def test_reminder_processing_expiry_and_rebind_retains_stable_ids(client):
    c, _ = client
    assert post(c, 'iphone-reminders', 'register').status_code == 200
    store = api.reminder_store()
    rid = str(uuid.uuid4())
    store.enqueue(DEVICE, rid, 'create', {'title': 'fixture'}, 'default')
    assert post(c, 'iphone-reminders', 'poll').json()['command']['id'] == rid
    with store.connection() as db:
        db.execute('UPDATE devices SET principal=? WHERE id=?', ('legacy', DEVICE))
    assert post(c, 'iphone-reminders', 'register', rebind=True).status_code == 200
    assert post(c, 'iphone-reminders', 'poll').json()['command']['id'] == rid
    with store.connection() as db:
        db.execute('UPDATE commands SET expires=0 WHERE id=?', (rid,))
    assert store.status(DEVICE, rid, 'default')['state'] == 'unconfirmed'
    assert post(c, 'iphone-reminders', 'poll').json()['command'] is None
    # Late device-confirmed result may settle uncertainty, never resend a write.
    assert post(c, 'iphone-reminders', 'ack', request_id=rid, state='completed', result={}).status_code == 200
    assert store.status(DEVICE, rid, 'default')['state'] == 'completed'


def test_memory_profiles_a_b_a(client):
    c, home = client
    b = home/'profiles'/'b'
    b.mkdir(parents=True)
    (b/'config.yaml').write_text('{}')
    assert c.post(P+'/memory/write', json={'section': 'soul', 'content': 'A'}).status_code == 200
    assert c.post(P+'/memory/write?profile=b', json={'section': 'soul', 'content': 'B'}).status_code == 200
    assert c.get(P+'/memory?profile=b').json()['soul'] == 'B'
    assert (b/'SOUL.md').read_text() == 'B'
    assert c.get(P+'/memory').json()['soul'] == 'A'
    assert post(c, 'photo-catalog', 'register', profile='b').status_code == 403
    response = c.post(P+'/photo-catalog/register?profile=b', json={'profile': 'b', 'device_id': DEVICE, 'secret': SECRET})
    assert response.status_code == 200
    # Integrations are root-scoped, memories are profile-scoped.
    assert (home/'webui/photo-catalog/catalog.sqlite3').is_file()
    assert not (b/'webui').exists()


def test_relocated_clis_use_retained_stores(client, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    c, home = client
    assert post(c, 'photo-catalog', 'register').status_code == 200
    assert post(c, 'iphone-reminders', 'register').status_code == 200
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'HERMES_HOME': str(home), 'TMPDIR': str(home)}
    def run(name, *args):
        command = [sys.executable, str(root/'plugins/cloudseed_mobile/scripts'/name), '--state-dir', str(home/'webui'), '--profile', 'default', *args]
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert run('photo-catalog', 'devices')['devices'][0]['id'] == DEVICE
    assert run('iphone-reminders', 'devices')['devices'][0]['id'] == DEVICE
    query = home/'request.json'
    query.write_text(json.dumps({'title': 'CLI synthetic'}))
    rid = str(uuid.uuid4())
    assert run('iphone-reminders', 'request', '--device', DEVICE, '--id', rid, '--operation', 'create', '--json-file', str(query))['state'] == 'queued'
    assert post(c, 'iphone-reminders', 'poll').json()['command']['id'] == rid
    assert run('iphone-reminders', 'status', '--device', DEVICE, '--id', rid)['state'] == 'processing'


@pytest.mark.parametrize('op', ['poll', 'ack', 'disconnect', 'status'])
def test_reminder_device_routes_require_secret(client, op):
    c, _ = client
    assert post(c, 'iphone-reminders', 'register').status_code == 200
    rid = str(uuid.uuid4())
    api.reminder_store().enqueue(DEVICE, rid, 'create', {'title': 'fixture'}, 'default')
    assert post(c, 'iphone-reminders', 'poll').status_code == 200
    response = c.post(P+'/iphone-reminders/'+op, json={'profile': 'default', 'device_id': DEVICE,
                      'request_id': rid, 'state': 'completed', 'result': {}})
    assert response.status_code == 400


def test_usage_identity_bound_and_no_exception_leak(client, monkeypatch):
    import json
    from agent import account_usage
    c, home = client
    (home/'auth.json').write_text(json.dumps({'credential_pool': {'anthropic': [
        {'id': 'usage-a', 'source': 'manual', 'priority': 0, 'auth_type': 'oauth', 'access_token': 'sk-ant-oat01-fixture-a'},
        {'id': 'usage-b', 'source': 'manual', 'priority': 1, 'auth_type': 'oauth', 'access_token': 'sk-ant-oat01-fixture-b'},
    ]}}))
    original = (home/'auth.json').read_bytes()
    seen = []
    def probe(url, headers, timeout):
        token = headers['Authorization']
        seen.append(token)
        if token.endswith('fixture-b'):
            raise RuntimeError('sk-ant-oat01-fixture-b PRIVATE RESPONSE')
        return {'five_hour': {'utilization': 25, 'resets_at': '2030-01-01T00:00:00Z'}}
    monkeypatch.setattr(account_usage, '_get_json', probe)
    result = c.get(P+'/provider/accounts/usage?provider=anthropic&refresh=1')
    assert result.status_code == 200
    section = next(p for p in result.json()['providers'] if p['id'] == 'anthropic')
    by_id = {a['id']: a['usage'] for a in section['accounts']}
    assert by_id['usage-a']['windows'][0]['used_percent'] == 25
    assert by_id['usage-b']['status'] == 'unavailable'
    assert set(seen) == {'Bearer sk-ant-oat01-fixture-a', 'Bearer sk-ant-oat01-fixture-b'}
    assert 'sk-ant-' not in result.text and 'PRIVATE RESPONSE' not in result.text
    assert (home/'auth.json').read_bytes() == original
    c.get(P+'/provider/accounts/usage?provider=anthropic&refresh=1')
    assert len(seen) == 2  # explicit refresh retry fence


def test_native_dashboard_discovery_mount_and_disable(client, monkeypatch):
    import sys
    from hermes_cli import web_server_dashboard as dashboard
    c, _ = client
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(dashboard, '_dashboard_plugin_search_dirs', lambda: [(root/'plugins', 'bundled')])
    entries = [p for p in dashboard._discover_dashboard_plugins() if p['name'] == 'cloudseed_mobile']
    assert len(entries) == 1
    # Native importer/mount path, with server shell isolated (not a runtime boot).
    app = FastAPI()
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', SimpleNamespace(app=app, _get_dashboard_plugins=lambda: entries))
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugins_cmd', SimpleNamespace(_get_enabled_set=lambda: set(), _get_disabled_set=lambda: set()))
    async def scope():
        yield
    monkeypatch.setattr(dashboard, '_plugin_route_secret_scope', scope)
    dashboard._mount_plugin_api_routes()
    with TestClient(app) as mounted:
        assert mounted.get(P+'/memory').status_code == 401  # plugin auth, not 404
    # Explicit disable must prevent backend mounting even though it is bundled.
    disabled_app = FastAPI()
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', SimpleNamespace(app=disabled_app, _get_dashboard_plugins=lambda: entries))
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugins_cmd', SimpleNamespace(_get_enabled_set=lambda: set(), _get_disabled_set=lambda: {'cloudseed_mobile'}))
    dashboard._mount_plugin_api_routes()
    with TestClient(disabled_app) as disabled:
        assert disabled.get(P+'/memory').status_code == 404
