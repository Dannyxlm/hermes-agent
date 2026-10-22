"""Desktop takeover contracts through public RPC with real temporary SQLite."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from tui_gateway import server
from tui_gateway.transport import Transport


class Peer(Transport):
    auth_identity = {'provider': 'fixture', 'user_id': 'owner'}

    def write(self, obj):
        return True

    def close(self):
        pass


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    from hermes_cli import profiles
    from tools.desktop_spaces import SpaceStore
    from tui_gateway.methods_desktop import _desktop_scope

    home = tmp_path / '.hermes'
    home.mkdir()
    secondary = home / 'profiles' / 'secondary'
    secondary.mkdir(parents=True)
    (home / 'config.yaml').write_text('cloudseed_mobile:\n  owner_provider: fixture\n  owner_user_id: owner\n')
    (secondary / 'config.yaml').write_text('{}\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(server, '_hermes_home', home)
    monkeypatch.setattr(server, '_profile_home', lambda name: secondary if name == 'secondary' else None)
    monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: home)
    peer = Peer()
    params = {'profile': 'default', 'space': 'desktop', 'connection_id': 'connection',
              'installation_id': 'installation-a'}

    def call(method, **extra):
        token = server.bind_transport(peer)
        try:
            return server.handle_request({'jsonrpc': '2.0', 'id': 'request', 'method': method,
                                          'params': {**params, **extra}})
        finally:
            server.reset_transport(token)

    token = server.bind_transport(peer)
    try:
        _, _, scope = _desktop_scope(server, params)
    finally:
        server.reset_transport(token)
    store = SpaceStore(home / 'desktop-spaces.sqlite')
    return SimpleNamespace(call=call, store=store, scope=scope, home=home, secondary=secondary)


class TestDesktopInitialization:
    def test_reader_never_observes_half_created_database(self, desktop, monkeypatch):
        import sqlite3
        import threading

        connect = sqlite3.connect
        reached, release = threading.Event(), threading.Event()
        first = True

        def paused_connect(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                reached.set()
                assert release.wait(10)
            return connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, 'connect', paused_connect)
        thread = threading.Thread(target=desktop.store.leases.initialize)
        thread.start()
        try:
            assert reached.wait(10)
            response = desktop.call('desktop.snapshot')
            assert response['result']['state'] == 'watch'
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive()


class TestDesktopOwnerScope:
    def test_request_visible_across_installations_not_profiles(self, desktop):
        d = desktop
        d.store.publish_signin('desktop', d.scope, request_id='req', revision=1,
                               origin='https://example.com', reason='Continue', expires_at=time.time() + 60)
        assert d.call('desktop.snapshot', installation_id='installation-b')['result']['signin']['id'] == 'req'
        assert d.call('desktop.snapshot', profile='secondary')['result']['signin'] is None

    def test_decision_reconciles_across_installations(self, desktop):
        d = desktop
        d.store.publish_signin('desktop', d.scope, request_id='req', revision=1,
                               origin='https://example.com', reason='Continue', expires_at=time.time() + 60)
        state = d.call('desktop.snapshot')['result']
        fields = {'action_id': 'act', 'action': 'not_now', 'request_id': 'req', 'request_revision': 1,
                  'expected_revision': state['revision'], 'expected_lease_epoch': 0}
        receipt = d.call('desktop.action', **fields)['result']
        assert receipt['status'] == 'accepted'
        assert d.call('desktop.action_status', action_id='act', installation_id='installation-b')['result'] == receipt
        assert d.call('desktop.action', **fields, installation_id='installation-b')['result'] == receipt


class TestDesktopAdmission:
    def test_fresh_none_lease_admits_agent_but_stale_epoch_does_not(self, desktop):
        assert desktop.store.leases.agent_permitted('desktop', 0)
        assert not desktop.store.leases.agent_permitted('desktop', 1)

    def test_human_lease_refuses_browser_before_supervisor_or_cli(self, desktop, monkeypatch):
        from tools import browser_use_cli as browser
        d = desktop
        d.store.leases.acquire('desktop', d.scope, 'viewer', expected_epoch=0, ttl=60)
        monkeypatch.setattr(browser, '_find_cli', lambda: ['fixture-harness'])
        monkeypatch.setattr(browser, '_base_subprocess_env', lambda: {'BU_CDP_URL': 'http://127.0.0.1:9225'})
        monkeypatch.setattr(browser, '_route_backend', lambda *a, **kw: None)
        attach, launch = Mock(), Mock(side_effect=AssertionError('must not launch'))
        monkeypatch.setattr(browser, '_attach_vault_supervisor', attach)
        monkeypatch.setattr(browser, '_run_cli_killing_process_group', launch)
        result = json.loads(browser.browser_exec('print(1)', task_id='task'))
        assert result['success'] is False
        assert result['error_type'] == 'human_has_control'
        attach.assert_not_called()
        launch.assert_not_called()

    def test_secret_write_refuses_ava_desktop_supervisor(self, desktop, monkeypatch):
        from tools import browser_vault_tool as vault
        desktop.store.leases.acquire('desktop', desktop.scope, 'viewer', expected_epoch=0, ttl=60)
        supervisor = SimpleNamespace(cdp_url='http://127.0.0.1:9225', evaluate_runtime=Mock(return_value={'ok': True}))
        monkeypatch.setattr(vault, '_ensure_supervisor', lambda tid: supervisor)
        result = vault._eval_js_secret('task', 'fixture expression')
        assert result['success'] is False
        assert result['error_type'] == 'human_has_control'
        supervisor.evaluate_runtime.assert_not_called()

    def test_snapshot_has_explicit_null_expiry(self, desktop):
        state = desktop.call('desktop.snapshot')['result']
        assert state['lease']['expires_at'] is None


class TestDesktopProducer:
    def test_request_signin_denies_meta_before_publish(self, desktop, monkeypatch):
        from tools import browser_vault_tool as vault
        from tools import browser_supervisor
        supervisor = SimpleNamespace(cdp_url='http://127.0.0.1:9225')
        monkeypatch.setattr(browser_supervisor.SUPERVISOR_REGISTRY, 'get', lambda tid: supervisor)
        monkeypatch.setattr(vault, '_current_page_origin', lambda tid: 'https://www.facebook.com')
        reply = json.loads(vault._handle_vault({'action': 'request_signin', 'reason': 'Continue'}, task_id='task'))
        assert reply['success'] is False
        assert reply['error_type'] == 'origin_denied'
        assert desktop.call('desktop.snapshot')['result']['signin'] is None
