"""Saved login with an empty RPC-process registry and a real fake CDP socket."""
import json
from unittest.mock import Mock

from tests.tools.test_browser_vault_signin import TestSavedLoginProducer as _Producer, start_producer
from tests.waiting import wait_until

pytest_plugins = ['tests.tools.test_browser_vault_signin']


class TestSavedLoginProcess:
    def test_published_target_is_stable_chrome_target(self, desktop, door, signin, signin_cdp):
        result, done, thread = start_producer(desktop.home)
        try:
            state = wait_until(lambda: _Producer._pending(desktop, 'default'))
            with desktop.store.leases.connection() as db:
                binding = desktop.store.signin_binding(db, 'desktop', desktop.scope)
                assert binding['target_id'] == 'chrome-target'
        finally:
            state = desktop.call('desktop.snapshot')['result']
            if state['signin']:
                _Producer._action(desktop, 'default', 'not_now', 'cleanup',
                                              request_id=state['signin']['id'], request_revision=1)
            assert done.wait(10)
            thread.join(2)

    def test_empty_rpc_registry_fills_exact_published_target_once(self, desktop, door, signin, signin_cdp, monkeypatch, caplog):
        from tools import browser_supervisor, browser_tool_session
        result, done, thread = start_producer(desktop.home)
        state = wait_until(lambda: _Producer._pending(desktop, 'default'))
        # This is the reviewer's cross-process fixture: no producer supervisor in the RPC process.
        monkeypatch.setattr(browser_supervisor.SUPERVISOR_REGISTRY, 'get', lambda task: None)
        daemon = Mock(return_value={'success': False})
        monkeypatch.setattr(browser_tool_session, '_run_browser_command', daemon)
        receipt = _Producer._action(desktop, 'default', 'saved_login', 'choose',
            request_id=state['signin']['id'], request_revision=1, login_handle=signin.meta.id)
        assert done.wait(10)
        thread.join(2)
        assert receipt['status'] == 'accepted'
        assert json.loads(result[0])['status'] == 'filled'
        assert signin_cdp.attached == ['chrome-target']
        assert signin_cdp.writes == 1
        assert desktop.call('desktop.action_status', action_id='choose')['result'] == receipt
        daemon.assert_not_called()
        assert signin.secret not in caplog.text + json.dumps(receipt) + str(result)
        assert signin.secret.encode() not in (desktop.home / 'desktop-spaces.sqlite').read_bytes()
