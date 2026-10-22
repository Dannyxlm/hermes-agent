"""Saved login with an empty RPC-process registry and a real fake CDP socket."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from websockets.sync.server import serve

from tests.tools.test_browser_vault_signin import TestSavedLoginProducer, start_producer
from tests.waiting import wait_until

pytest_plugins = ['tests.tools.test_browser_vault_signin']


@pytest.fixture
def signin_cdp(signin, monkeypatch):
    from tools import browser_supervisor, browser_tool_cdp

    page = SimpleNamespace(target='chrome-target', present=True, commands=[], attached=[], writes=0)

    def target_info():
        return {'targetId': page.target, 'type': 'page', 'url': signin.page.origin + '/login'}

    def capture_call(method, params=None, **kwargs):
        if method == 'Target.getTargetInfo':
            return {'result': {'targetInfo': target_info()}}
        if method == 'Runtime.evaluate':
            return {'result': {'result': {'type': 'string', 'value': signin.page.origin + '/login'}}}
        raise AssertionError(method)

    monkeypatch.setattr(browser_supervisor.SUPERVISOR_REGISTRY, 'capture',
                        lambda task: SimpleNamespace(page_session_id='page-session', call=capture_call))

    def handler(ws):
        for raw in ws:
            request = json.loads(raw)
            method, params = request['method'], request.get('params', {})
            page.commands.append(method)
            if method == 'Target.getTargetInfo' and page.present and params.get('targetId') == page.target:
                result = {'targetInfo': target_info()}
            elif method == 'Target.attachToTarget' and page.present and params.get('targetId') == page.target:
                page.attached.append(params['targetId'])
                result = {'sessionId': 'rpc-new-session'}
            elif method == 'Runtime.evaluate' and request.get('sessionId') == 'rpc-new-session':
                expression = params['expression']
                if signin.secret in expression:
                    page.writes += 1
                value = signin.supervisor.evaluate_runtime(expression)['result']
                result = {'result': {'type': 'object', 'value': value}}
            elif method == 'Target.detachFromTarget':
                result = {}
            else:
                ws.send(json.dumps({'id': request['id'], 'error': {'message': 'fixture target unavailable'}}))
                continue
            ws.send(json.dumps({'id': request['id'], 'result': result}))

    with serve(handler, '127.0.0.1', 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.socket.getsockname()[1]
        monkeypatch.setattr(browser_tool_cdp, '_resolve_cdp_override',
                            lambda endpoint: f'ws://127.0.0.1:{port}/devtools/browser/fixture')
        try:
            yield page
        finally:
            server.shutdown()
            thread.join(5)


class TestSavedLoginProcess:
    def test_published_target_is_stable_chrome_target(self, desktop, door, signin, signin_cdp):
        result, done, thread = start_producer(desktop.home)
        try:
            state = wait_until(lambda: TestSavedLoginProducer._pending(desktop, 'default'))
            with desktop.store.leases.connection() as db:
                binding = desktop.store.signin_binding(db, 'desktop', desktop.scope)
                assert binding['target_id'] == 'chrome-target'
        finally:
            state = desktop.call('desktop.snapshot')['result']
            if state['signin']:
                TestSavedLoginProducer._action(desktop, 'default', 'not_now', 'cleanup',
                                              request_id=state['signin']['id'], request_revision=1)
            assert done.wait(10)
            thread.join(2)

    def test_empty_rpc_registry_fills_exact_published_target_once(self, desktop, door, signin, signin_cdp, monkeypatch, caplog):
        from tools import browser_supervisor, browser_tool_session
        result, done, thread = start_producer(desktop.home)
        state = wait_until(lambda: TestSavedLoginProducer._pending(desktop, 'default'))
        # This is the reviewer's cross-process fixture: no producer supervisor in the RPC process.
        monkeypatch.setattr(browser_supervisor.SUPERVISOR_REGISTRY, 'get', lambda task: None)
        daemon = Mock(return_value={'success': False})
        monkeypatch.setattr(browser_tool_session, '_run_browser_command', daemon)
        receipt = TestSavedLoginProducer._action(desktop, 'default', 'saved_login', 'choose',
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
