"""Agent browser admission and reconnection across a shared Desktop lease."""
import json
import subprocess
from unittest.mock import Mock

from tests.tui_gateway import test_desktop_door as helpers

pytest_plugins = ['tests.tui_gateway.test_desktop_door']


class TestDesktopBrowserReconnect:
    def test_handback_reloads_cut_harness_before_next_execution(self, desktop, door, monkeypatch):
        from tools import browser_use_cli as browser, browser_supervisor

        monkeypatch.setattr(browser, '_find_cli', lambda: ['fixture-harness'])
        monkeypatch.setattr(browser, '_base_subprocess_env', lambda: {'BU_CDP_URL': 'http://127.0.0.1:9225'})
        monkeypatch.setattr(browser, '_route_backend', lambda *a, **kw: None)
        attach = Mock()
        monkeypatch.setattr(browser, '_attach_vault_supervisor', attach)
        stop = Mock()
        monkeypatch.setattr(browser_supervisor.SUPERVISOR_REGISTRY, 'stop', stop)
        launches = []

        def run(cmd, code, env, timeout):
            launches.append((cmd, code))
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        monkeypatch.setattr(browser, '_run_cli_killing_process_group', run)
        assert json.loads(browser.browser_exec('print(1)', session='reconnect', task_id='reconnect-task'))['success']
        assert helpers.watch(desktop)['result']['mode'] == 'watch'
        assert helpers.action(desktop, 'take_over', viewer_id='viewer')['result']['status'] == 'accepted'
        assert json.loads(browser.browser_exec('print(2)', session='reconnect', task_id='reconnect-task'))['error_type'] == 'human_has_control'
        assert len(launches) == 1
        assert helpers.action(desktop, 'hand_back', action_id='back', viewer_id='viewer')['result']['status'] == 'accepted'
        assert json.loads(browser.browser_exec('print(3)', session='reconnect', task_id='reconnect-task'))['success']
        assert [cmd for cmd, _ in launches] == [['fixture-harness'], ['fixture-harness', '--reload'], ['fixture-harness']]
        stop.assert_called_once_with('reconnect-task')

    def test_confirmed_restart_reconciles_before_first_agent_connection(self, desktop, door, monkeypatch):
        from tools import browser_use_cli as browser
        door.state['epoch'] = 100
        monkeypatch.setattr(browser, '_find_cli', lambda: ['fixture-harness'])
        monkeypatch.setattr(browser, '_base_subprocess_env', lambda: {'BU_CDP_URL': 'http://127.0.0.1:9225'})
        monkeypatch.setattr(browser, '_route_backend', lambda *a, **kw: None)
        monkeypatch.setattr(browser, '_attach_vault_supervisor', Mock())
        monkeypatch.setattr(browser, '_run_cli_killing_process_group', lambda *a, **kw:
                            subprocess.CompletedProcess(a[0], 0, stdout='', stderr=''))
        result = json.loads(browser.browser_exec('print(1)', task_id='restart'))
        assert result['success'] is True
        assert desktop.store.leases.read('desktop').epoch == 100

    def test_parallel_snapshot_reconcile_does_not_deny_agent(self, desktop, door, monkeypatch):
        from tools import browser_use_cli as browser
        from tools.desktop_space_lease import LeaseStore

        monkeypatch.setattr(browser, '_find_cli', lambda: ['fixture-harness'])
        monkeypatch.setattr(browser, '_base_subprocess_env', lambda: {'BU_CDP_URL': 'http://127.0.0.1:9225'})
        monkeypatch.setattr(browser, '_route_backend', lambda *a, **kw: None)
        monkeypatch.setattr(browser, '_attach_vault_supervisor', Mock())
        monkeypatch.setattr(browser, '_run_cli_killing_process_group', lambda *a, **kw:
                            subprocess.CompletedProcess(a[0], 0, stdout='', stderr=''))
        read = LeaseStore.read
        reads = 0

        def interleaved_read(store, resource):
            nonlocal reads
            result = read(store, resource)
            reads += 1
            if reads == 2:
                assert desktop.call('desktop.snapshot')['result']['lease']['holder'] == 'agent'
            return result

        monkeypatch.setattr(LeaseStore, 'read', interleaved_read)
        assert json.loads(browser.browser_exec('print(1)', task_id='parallel'))['success'] is True

    def test_configured_but_unreachable_door_refuses_even_fresh_lease(self, desktop, door, monkeypatch):
        from tools import browser_use_cli as browser
        door.fail = True
        monkeypatch.setattr(browser, '_find_cli', lambda: ['fixture-harness'])
        monkeypatch.setattr(browser, '_base_subprocess_env', lambda: {'BU_CDP_URL': 'http://127.0.0.1:9225'})
        monkeypatch.setattr(browser, '_route_backend', lambda *a, **kw: None)
        launch = Mock(side_effect=AssertionError('must not launch'))
        monkeypatch.setattr(browser, '_run_cli_killing_process_group', launch)
        assert json.loads(browser.browser_exec('print(1)', task_id='fresh'))['error_type'] == 'human_has_control'
        launch.assert_not_called()
