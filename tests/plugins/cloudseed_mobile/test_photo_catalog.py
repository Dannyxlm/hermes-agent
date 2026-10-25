"""Catalog setup is process-scoped; metadata reads cannot acquire writer locks."""
import base64
import json
import sqlite3
import uuid

import pytest
from plugins.cloudseed_mobile import photo_catalog as photos

DEVICE = str(uuid.UUID(int=1))
SECRET = 'a' * 64


def traced(monkeypatch):
    statements = []
    connections = []
    real = sqlite3.connect
    def connect(database, *args, **kwargs):
        db = real(database, *args, **kwargs)
        db.set_trace_callback(statements.append)
        connections.append((str(database), kwargs))
        return db
    monkeypatch.setattr(photos.sqlite3, 'connect', connect)
    return statements, connections


def item(i):
    return {'id': f'{i:064x}', 'fingerprint': 'b' * 64, 'created': i,
            'albums': ['Fixture'], 'text': 'cat', 'favorite': False,
            'screenshot': False, 'preview': base64.b64encode(b'\xff\xd8\xff\xff\xd9').decode()}


def test_sequential_request_construction_sets_up_schema_once(tmp_path, monkeypatch):
    statements, _ = traced(monkeypatch)
    path = tmp_path / 'catalog.sqlite3'
    def catalog():
        return photos.PhotoCatalog(path, free_bytes=lambda: 10 * 1024**3)
    store = catalog()
    store.register('owner', 'default', DEVICE, SECRET)
    epoch = store.begin('owner', 'default', DEVICE, SECRET, 100)['epoch']
    store.manifest('owner', 'default', DEVICE, SECRET, epoch, [item(i) for i in range(100)])
    for i in range(100):
        assert catalog().put('owner', 'default', DEVICE, SECRET, epoch, item(i)) == {'ok': True, 'protocol_version': 1}
    assert catalog().status('default', DEVICE)['count'] == 100
    assert sum('CREATE TABLE IF NOT EXISTS devices' in sql for sql in statements) == 1


@pytest.mark.parametrize('operation', ['status', 'devices', 'search', 'preview'])
def test_catalog_reads_use_readonly_connections(tmp_path, monkeypatch, operation):
    store = photos.PhotoCatalog(tmp_path / 'catalog.sqlite3', free_bytes=lambda: 10 * 1024**3)
    store.register('owner', 'default', DEVICE, SECRET)
    epoch = store.begin('owner', 'default', DEVICE, SECRET, 1)['epoch']
    store.manifest('owner', 'default', DEVICE, SECRET, epoch, [item(1)])
    store.put('owner', 'default', DEVICE, SECRET, epoch, item(1))
    calls = {'status': lambda: store.status('default', DEVICE),
             'devices': lambda: store.devices('default'),
             'search': lambda: store.search('default', DEVICE, {'text': 'cat'}),
             'preview': lambda: store.preview('default', DEVICE, item(1)['id'])}
    # The old read path used the same projections under a writable transaction.
    connection = store.connection
    with monkeypatch.context() as old:
        old.setattr(store, 'connection', lambda readonly=False: connection())
        expected = calls[operation]()
    statements, connections = traced(monkeypatch)
    result = calls[operation]()
    if operation == 'preview':
        assert result == expected
    else:
        assert json.dumps(result).encode() == json.dumps(expected).encode()
    assert all('mode=ro' in path and kwargs.get('uri') for path, kwargs in connections)
    assert not any(sql.upper().startswith(('BEGIN IMMEDIATE', 'INSERT', 'UPDATE', 'DELETE', 'CREATE')) for sql in statements)


def test_schema_guard_reinitializes_for_schema_version_or_replaced_file(tmp_path, monkeypatch):
    statements, _ = traced(monkeypatch)
    path = tmp_path / 'catalog.sqlite3'
    photos.PhotoCatalog(path)
    photos.PhotoCatalog(path)
    assert sum('CREATE TABLE IF NOT EXISTS devices' in sql for sql in statements) == 1
    monkeypatch.setattr(photos, 'SCHEMA_VERSION', photos.SCHEMA_VERSION + 1)
    photos.PhotoCatalog(path)
    photos.PhotoCatalog(path)
    assert sum('CREATE TABLE IF NOT EXISTS devices' in sql for sql in statements) == 2
    path.rename(tmp_path / 'old.sqlite3')
    photos.PhotoCatalog(path)
    photos.PhotoCatalog(path)
    assert sum('CREATE TABLE IF NOT EXISTS devices' in sql for sql in statements) == 3


def baseline(store, device=DEVICE, count=2):
    store.register('owner', 'default', device, SECRET)
    epoch = store.begin('owner', 'default', device, SECRET, count)['epoch']
    store.manifest('owner', 'default', device, SECRET, epoch, [item(i) for i in range(count)])
    for i in range(count):
        store.put('owner', 'default', device, SECRET, epoch, item(i))
    assert store.finish('owner', 'default', device, SECRET, epoch)['complete']
    return epoch


def catalog_state(store):
    with store.connection(readonly=True) as db:
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('devices', 'manifest', 'photos')}


def test_delta_put_without_finish_preserves_baseline_and_replays(tmp_path):
    path = tmp_path / 'catalog.sqlite3'
    store = photos.PhotoCatalog(path, free_bytes=lambda: 10 * 1024**3)
    epoch = baseline(store)
    cut = [dict(id=item(2)['id'], fingerprint=item(2)['fingerprint'])]
    expected = {'ok': True, 'epoch': epoch, 'missing': [item(2)['id']],
                'applied': {'upserts': 1, 'deletes': 0}}
    assert store.delta('owner', 'default', DEVICE, SECRET, epoch, cut, []) == expected
    state = catalog_state(store)
    assert store.delta('owner', 'default', DEVICE, SECRET, epoch, cut, []) == expected
    assert catalog_state(store) == state
    status = store.status('default', DEVICE)
    assert (status['epoch'], status['count'], status['total'], status['complete']) == (epoch, 2, 3, False)
    with pytest.raises(photos.PhotoError):
        store.put('owner', 'default', DEVICE, SECRET, epoch, {**item(2), 'fingerprint': 'c' * 64})
    store.put('owner', 'default', DEVICE, SECRET, epoch, item(2))
    status = store.status('default', DEVICE)
    assert (status['epoch'], status['count'], status['total'], status['complete']) == (epoch, 3, 3, True)
    assert {p['id'] for p in store.search('default', DEVICE, {})['items']} == {item(i)['id'] for i in range(3)}
    assert store.preview('default', DEVICE, item(0)['id']) == base64.b64decode(item(0)['preview'])
    expected['missing'] = []
    assert photos.PhotoCatalog(path).delta('owner', 'default', DEVICE, SECRET, epoch, cut, []) == expected
    assert store.status('default', DEVICE) == status


def test_delta_delete_is_device_scoped_and_works_without_reserve(tmp_path):
    store = photos.PhotoCatalog(tmp_path / 'catalog.sqlite3', free_bytes=lambda: 10 * 1024**3)
    epoch = baseline(store)
    other = str(uuid.UUID(int=2))
    baseline(store, other)
    store.free_bytes = lambda: 0
    deleted = item(0)['id']
    result = store.delta('owner', 'default', DEVICE, SECRET, epoch, [], [deleted])
    assert result == {'ok': True, 'epoch': epoch, 'missing': [], 'applied': {'upserts': 0, 'deletes': 1}}
    state = catalog_state(store)
    assert store.delta('owner', 'default', DEVICE, SECRET, epoch, [], [deleted]) == result
    assert catalog_state(store) == state
    status = store.status('default', DEVICE)
    assert (status['count'], status['total'], status['complete']) == (1, 1, True)
    assert [p['id'] for p in store.search('default', DEVICE, {'text': 'cat'})['items']] == [item(1)['id']]
    with pytest.raises(photos.PhotoError, match='Photo unavailable'):
        store.preview('default', DEVICE, deleted)
    assert store.preview('default', other, deleted) == base64.b64decode(item(0)['preview'])
    assert store.status('default', other)['count'] == 2
    with store.connection(readonly=True) as db:
        for table in ('manifest', 'photos'):
            assert db.execute(f'SELECT 1 FROM {table} WHERE device=? AND asset=?', (DEVICE, deleted)).fetchone() is None
        indexed = {r[0] for r in db.execute("SELECT rowid FROM photo_text WHERE photo_text MATCH 'cat'")}
        assert indexed == {r[0] for r in db.execute('SELECT rowid FROM photos')}


def test_delta_changed_fingerprint_and_mixed_replay(tmp_path):
    store = photos.PhotoCatalog(tmp_path / 'catalog.sqlite3', free_bytes=lambda: 10 * 1024**3)
    epoch = baseline(store)
    changed = {**item(0), 'fingerprint': 'c' * 64, 'text': 'dog'}
    upserts = [{'id': changed['id'], 'fingerprint': changed['fingerprint']},
               {'id': item(2)['id'], 'fingerprint': item(2)['fingerprint']}]
    for _ in range(2):
        result = store.delta('owner', 'default', DEVICE, SECRET, epoch, upserts, [item(1)['id']])
        assert result['missing'] == [changed['id'], item(2)['id']]
        status = store.status('default', DEVICE)
        assert (status['count'], status['total'], status['complete']) == (0, 2, False)
    store.free_bytes = lambda: 0
    assert store.delta('owner', 'default', DEVICE, SECRET, epoch, upserts, [item(1)['id']]) == result
    with pytest.raises(photos.PhotoStorageFull):
        store.put('owner', 'default', DEVICE, SECRET, epoch, changed)
    store.free_bytes = lambda: 10 * 1024**3
    for payload in (changed, item(2)):
        store.put('owner', 'default', DEVICE, SECRET, epoch, payload)
    assert store.status('default', DEVICE)['complete']
    assert [p['id'] for p in store.search('default', DEVICE, {'text': 'dog'})['items']] == [changed['id']]
    assert [p['id'] for p in store.search('default', DEVICE, {'text': 'cat'})['items']] == [item(2)['id']]


def test_delta_rolls_back_entire_cut_on_write_failure(tmp_path, monkeypatch):
    store = photos.PhotoCatalog(tmp_path / 'catalog.sqlite3', free_bytes=lambda: 10 * 1024**3)
    epoch = baseline(store)
    with store.connection() as db:
        db.execute("CREATE TRIGGER reject_manifest BEFORE INSERT ON manifest BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    before = catalog_state(store)
    statements, _ = traced(monkeypatch)
    with pytest.raises(sqlite3.IntegrityError, match='fixture'):
        store.delta('owner', 'default', DEVICE, SECRET, epoch, [item(2)], [item(0)['id']])
    assert statements.count('BEGIN IMMEDIATE') == 1
    assert 'ROLLBACK' in statements and 'COMMIT' not in statements
    assert catalog_state(store) == before


def test_delta_accepts_finished_baseline_with_pending_payloads_and_empty_baseline(tmp_path):
    store = photos.PhotoCatalog(tmp_path / 'catalog.sqlite3', free_bytes=lambda: 10 * 1024**3)
    epoch = baseline(store, count=0)
    assert store.delta('owner', 'default', DEVICE, SECRET, epoch, [], []) == {
        'ok': True, 'epoch': epoch, 'missing': [], 'applied': {'upserts': 0, 'deletes': 0}}
    epoch = store.begin('owner', 'default', DEVICE, SECRET, 1)['epoch']
    store.manifest('owner', 'default', DEVICE, SECRET, epoch, [item(0)])
    assert not store.finish('owner', 'default', DEVICE, SECRET, epoch)['complete']
    store.delta('owner', 'default', DEVICE, SECRET, epoch, [item(1)], [])
    assert store.status('default', DEVICE)['total'] == 2
