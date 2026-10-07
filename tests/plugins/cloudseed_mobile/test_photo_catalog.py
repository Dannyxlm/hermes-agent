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
