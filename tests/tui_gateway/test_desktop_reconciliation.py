"""Desktop reconnect/retention contracts through public dispatch and real SQLite."""
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.transport import Transport


class Peer(Transport):
    def __init__(self):
        self.auth_identity = {'provider': 'fixture', 'user_id': 'owner'}
        self.frames = []
        self.done = threading.Event()

    def write(self, obj):
        self.frames.append(obj)
        self.done.set()
        return True

    def close(self):
        pass


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    from hermes_cli import profiles
    from tools import desktop_spaces
    from tui_gateway.methods_desktop import _desktop_scope

    home = tmp_path / '.hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('cloudseed_mobile:\n  owner_provider: fixture\n  owner_user_id: owner\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(server, '_hermes_home', home)
    monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: home)
    clock = SimpleNamespace(value=10_000_000.0)
    monkeypatch.setattr(desktop_spaces, 'time', SimpleNamespace(time=lambda: clock.value))
    params = {'profile': 'default', 'space': 'studio', 'connection_id': 'connection',
              'installation_id': 'installation'}
    a, b = Peer(), Peer()

    def call(peer, method, **extra):
        peer.done.clear()
        assert server.dispatch({'jsonrpc': '2.0', 'id': 'request', 'method': method,
                                'params': {**params, **extra}}, peer) is None
        assert peer.done.wait(10), method
        return peer.frames[-1]

    token = server.bind_transport(a)
    try:
        _, _, scope = _desktop_scope(server, params)
    finally:
        server.reset_transport(token)
    store = desktop_spaces.SpaceStore(home / 'desktop-spaces.sqlite')
    return SimpleNamespace(call=call, a=a, b=b, store=store, scope=scope, clock=clock,
                           path=home / 'desktop-spaces.sqlite')


def publish(desktop, request='req', origin='https://example.com'):
    desktop.store.publish_signin('studio', desktop.scope, request_id=request, revision=1,
        origin=origin, reason='Continue', expires_at=desktop.clock.value + 60)
    state = desktop.call(desktop.a, 'desktop.snapshot')['result']
    return {'action_id': 'decision', 'action': 'not_now', 'request_id': request, 'request_revision': 1,
            'expected_revision': state['revision'], 'expected_lease_epoch': state['lease']['epoch']}


class TestDesktopReconnect:
    @pytest.mark.parametrize('check', ['status', 'resend', 'snapshot'])
    def test_same_owner_reconnect_preserves_decision(self, desktop, check):
        d = desktop
        fields = publish(d)
        receipt = d.call(d.a, 'desktop.action', **fields)['result']
        assert receipt['status'] == 'accepted'
        publish(d, request='next-request')
        if check == 'status':
            assert d.call(d.b, 'desktop.action_status', action_id='decision')['result'] == receipt
        elif check == 'resend':
            assert d.call(d.b, 'desktop.action', **fields)['result'] == receipt
            with sqlite3.connect(d.path) as db:
                assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 1
        else:
            state = d.call(d.b, 'desktop.snapshot')['result']
            assert state['signin']['id'] == 'next-request'
            assert [event['label'] for event in state['activity']] == ['Sign-in deferred']

    def test_connection_selector_is_not_durable_identity(self, desktop):
        d = desktop
        fields = publish(d)
        receipt = d.call(d.a, 'desktop.action', **fields)['result']
        assert d.call(d.b, 'desktop.action_status', action_id='decision', connection_id='reconnected')['result'] == receipt
        assert d.call(d.b, 'desktop.action', **fields, connection_id='reconnected')['result'] == receipt

    @pytest.mark.parametrize('changed', [{'request_id': 'other'}, {'request_revision': 2},
        {'expected_revision': 99}, {'expected_lease_epoch': 99}])
    def test_different_intent_does_not_replace_receipt(self, desktop, changed):
        d = desktop
        fields = publish(d)
        accepted = d.call(d.a, 'desktop.action', **fields)['result']
        result = d.call(d.b, 'desktop.action', **{**fields, **changed})['result']
        assert result['status'] == 'conflict'
        assert d.call(d.b, 'desktop.action_status', action_id='decision')['result'] == accepted
        with sqlite3.connect(d.path) as db:
            assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 1


class TestDesktopRetention:
    def test_expired_reads_are_filtered_without_writing(self, desktop):
        d = desktop
        fields = publish(d)
        assert d.call(d.a, 'desktop.action', **fields)['result']['status'] == 'accepted'
        d.clock.value += 30 * 24 * 60 * 60 + 1
        before = d.path.read_bytes()
        assert d.call(d.a, 'desktop.action_status', action_id='decision')['result']['status'] == 'unknown'
        assert d.call(d.a, 'desktop.snapshot')['result']['activity'] == []
        assert d.path.read_bytes() == before

    def test_decision_write_prunes_age_and_per_scope_cap(self, desktop):
        from tools.desktop_space_lease import digest
        d = desktop
        fields = publish(d)
        scoped = digest(d.scope)
        old = d.clock.value - 30 * 24 * 60 * 60 - 1
        with sqlite3.connect(d.path) as db:
            for scope in (scoped, 'other-scope'):
                db.executemany('INSERT INTO receipts VALUES(?,?,?,?,?)', [
                    ('studio', scope, f'act{i}', 'fingerprint', json.dumps({
                        'action_id': f'act{i}', 'status': 'conflict', 'revision': 2,
                        'lease_epoch': 0, 'server_time': old if i == 0 else d.clock.value - i}))
                    for i in range(1002)])
                db.executemany('INSERT INTO activity VALUES(?,?,?,?,?)', [
                    (f'{scope}-{i}', 'studio', scope, 'Sign-in deferred', old if i == 0 else d.clock.value - i)
                    for i in range(1002)])
        assert d.call(d.a, 'desktop.action', **fields)['result']['status'] == 'accepted'
        with sqlite3.connect(d.path) as db:
            assert db.execute('SELECT count(*) FROM receipts WHERE scope_hash=?', (scoped,)).fetchone()[0] == 1000
            assert db.execute('SELECT count(*) FROM activity WHERE scope_hash=?', (scoped,)).fetchone()[0] == 1000
            assert db.execute("SELECT count(*) FROM receipts WHERE action_id='act0'").fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM activity WHERE at=?', (old,)).fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM receipts WHERE scope_hash=?', ('other-scope',)).fetchone()[0] == 1001
        assert d.call(d.b, 'desktop.action_status', action_id='decision')['result']['status'] == 'accepted'


class TestDesktopSnapshotLimit:
    @pytest.mark.parametrize('limit', [1, 25, 100])
    def test_limit_bounds_activity(self, desktop, limit):
        from tools.desktop_space_lease import digest
        d = desktop
        publish(d)
        with sqlite3.connect(d.path) as db:
            db.executemany('INSERT INTO activity VALUES(?,?,?,?,?)', [
                (str(i), 'studio', digest(d.scope), 'Sign-in deferred', d.clock.value - i) for i in range(30)])
        state = d.call(d.a, 'desktop.snapshot', limit=limit)['result']
        assert len(state['activity']) == min(limit, 30)
        assert [event['at'] for event in state['activity']] == sorted(
            [event['at'] for event in state['activity']], reverse=True)


class TestDesktopInternalErrors:
    def test_internal_type_error_is_not_invalid_client_request(self, desktop, monkeypatch):
        from tools.desktop_spaces import SpaceStore
        def fail(*args, **kwargs):
            raise TypeError('internal detail')
        monkeypatch.setattr(SpaceStore, 'snapshot', fail)
        reply = desktop.call(desktop.a, 'desktop.snapshot')
        assert reply['error'] == {'code': -32603, 'message': 'Desktop operation failed'}


class TestDesktopOrigin:
    def test_default_https_port_is_canonical_in_snapshot(self, desktop):
        publish(desktop, origin='https://example.com:443')
        state = desktop.call(desktop.a, 'desktop.snapshot')['result']
        assert state['signin']['origin'] == 'https://example.com'
