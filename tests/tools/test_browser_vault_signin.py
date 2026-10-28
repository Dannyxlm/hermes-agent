"""Saved-login producer and model-blind fill through real tool and RPC entrypoints."""
import contextvars
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from websockets.sync.server import serve

import pytest

from tests.waiting import wait_until

pytest_plugins = ['tests.tui_gateway.test_desktop_door']


@pytest.fixture
def signin(desktop, monkeypatch, tmp_path):
    from agent.vault_store import VaultStore
    from agent.vault_backends.local import LocalLoginBackend
    from tools import browser_supervisor, browser_vault_signin

    secret = 'fixture-signin-password-not-real'
    store = VaultStore(tmp_path / 'vault')
    meta = store.add_item('login', 'Fixture', {'identifier_type': 'email', 'identifier': 'alex@example.com',
                                             'password': secret}, origin='https://example.com')
    store.add_item('login', 'Foreign', {'identifier_type': 'email', 'identifier': 'other@example.com',
                                      'password': 'fixture-other'}, origin='https://other.example.com')
    monkeypatch.setattr('agent.vault_store.get_vault_store', lambda: store)
    monkeypatch.setattr('agent.vault_backends.enabled_backends', lambda: [LocalLoginBackend()])
    page = SimpleNamespace(origin='https://example.com', writes=0)

    def evaluate(expression):
        if expression == 'window.location.href':
            return {'ok': True, 'result': page.origin + '/login'}
        if secret in expression:
            page.writes += 1
            return {'ok': True, 'result': {'filled': 1}}
        return {'ok': True, 'result': [{'autocomplete': 'current-password', 'formIndex': 0,
                                      'index': 0, 'label': '', 'name': 'password', 'type': 'password'}]}

    supervisor = SimpleNamespace(cdp_url='http://127.0.0.1:9225', _page_session_id='page-session',
                                 evaluate_runtime=evaluate)
    monkeypatch.setattr(browser_supervisor.SUPERVISOR_REGISTRY, 'get', lambda tid: supervisor)
    real_request = browser_vault_signin.request_signin
    monkeypatch.setattr(browser_vault_signin, 'request_signin',
                        lambda reason, task_id, timeout_s=5: real_request(reason, task_id, timeout_s=timeout_s))
    return SimpleNamespace(secret=secret, meta=meta, page=page, supervisor=supervisor)


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


def start_producer(home):
    from tui_gateway import server
    from tools import browser_vault_tool

    result = []
    done = threading.Event()

    def run():
        try:
            with server._session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
                result.append(browser_vault_tool.registry.dispatch('browser_vault', {'action': 'request_signin', 'reason': 'Read the report'}, task_id='producer-task'))
        finally:
            done.set()

    context = contextvars.copy_context()
    thread = threading.Thread(target=lambda: context.run(run), daemon=True)
    thread.start()
    return result, done, thread


class TestSavedLoginProducer:
    @pytest.fixture(autouse=True)
    def exact_cdp(self, signin_cdp):
        pass

    @pytest.mark.parametrize('profile', ['default', 'secondary'])
    @pytest.mark.parametrize('decision', ['saved_login', 'not_now', 'hand_back', 'heartbeat'])
    def test_producer_publishes_to_both_installations_and_resumes(self, desktop, door, signin, profile, decision, caplog):
        d = desktop
        home = d.home if profile == 'default' else d.secondary
        result, done, thread = start_producer(home)
        def published():
            assert not done.is_set(), result
            return self._pending(d, profile)

        state = wait_until(published, what='sign-in published')
        assert 'producer' not in state['unavailable']
        assert state['signin']['login_handle'] == signin.meta.id
        assert state['signin']['masked_identifier'] == 'a•••@example.com'
        assert 'saved_login' in state['signin']['allowed_actions']
        assert d.call('desktop.snapshot', profile='secondary' if profile == 'default' else 'default')['result']['signin'] is None
        if decision in ('hand_back', 'heartbeat'):
            watch = d.call('desktop.observe', profile=profile, mode='watch', expected_lease_epoch=state['lease']['epoch'])['result']
            taken = self._action(d, profile, 'take_over', 'take', viewer_id=watch['viewer_id'])
            assert taken['status'] == 'accepted'
            if decision == 'hand_back':
                assert self._action(d, profile, 'hand_back', 'back', viewer_id=watch['viewer_id'])['status'] == 'accepted'
            else:
                from tools import browser_vault_tool
                door.state.update(holder='agent', state='WATCHING', epoch=taken['lease_epoch'] + 1,
                                  human_viewer_id=None, expires_at=None, sessions=[])
                door.state['gate']['state'] = 'open'
                assert browser_vault_tool._eval_js_secret('another-task', 'fixture-only-no-input')['success']
            expected = 'human_done'
        else:
            signin_fields = {'request_id': state['signin']['id'], 'request_revision': state['signin']['revision']}
            if decision == 'saved_login':
                signin_fields['login_handle'] = signin.meta.id
            receipt = self._action(d, profile, decision, 'choose', **signin_fields)
            assert receipt['status'] == 'accepted'
            expected = 'filled' if decision == 'saved_login' else 'deferred'
        assert done.wait(10)
        thread.join(2)
        assert json.loads(result[0])['status'] == expected
        assert signin.page.writes == (1 if decision == 'saved_login' else 0)
        public = json.dumps(d.call('desktop.snapshot', profile=profile)) + str(result) + caplog.text
        assert signin.secret not in public
        assert signin.secret.encode() not in (d.home / 'desktop-spaces.sqlite').read_bytes()

    def test_expiry_resumes_without_input(self, desktop, door, signin):
        from tools.browser_vault_signin import request_signin
        result = json.loads(request_signin('Continue', 'producer-task', timeout_s=0.02))
        assert result['status'] == 'expired'
        assert signin.page.writes == 0
        assert desktop.call('desktop.snapshot')['result']['signin'] is None

    def test_no_exact_origin_login_never_offers_saved_login(self, desktop, door, signin, monkeypatch):
        monkeypatch.setattr('agent.vault_backends.enabled_backends', list)
        result, done, thread = start_producer(desktop.home)
        state = wait_until(lambda: self._pending(desktop, 'default'))
        assert state['signin']['login_handle'] is None
        assert state['signin']['masked_identifier'] is None
        assert state['signin']['allowed_actions'] == ['take_over', 'not_now']
        assert 'producer' not in state['unavailable']
        assert self._action(desktop, 'default', 'not_now', 'defer', request_id=state['signin']['id'],
                            request_revision=1)['status'] == 'accepted'
        assert done.wait(10)
        thread.join(2)
        assert json.loads(result[0])['status'] == 'deferred'
        assert signin.page.writes == 0

    @staticmethod
    def _pending(d, profile):
        state = d.call('desktop.snapshot', profile=profile, installation_id='phone-b')['result']
        return state if state['signin'] is not None else None

    @staticmethod
    def _action(d, profile, verb, action_id, **fields):
        state = d.call('desktop.snapshot', profile=profile)['result']
        return d.call('desktop.action', profile=profile, action=verb, action_id=action_id,
                      expected_revision=state['revision'], expected_lease_epoch=state['lease']['epoch'], **fields)['result']

    @pytest.mark.parametrize('change', ['origin', 'target'])
    def test_changed_origin_or_target_never_calls_vault_fill(self, desktop, door, signin, signin_cdp, monkeypatch, change):
        from tools import browser_vault_tool
        result, done, thread = start_producer(desktop.home)
        state = wait_until(lambda: self._pending(desktop, 'default'))
        if change == 'origin':
            signin.page.origin = 'https://other.example.com'
        else:
            signin_cdp.present = False
        fill = Mock(side_effect=AssertionError('must not fill'))
        monkeypatch.setattr(browser_vault_tool, 'browser_vault_fill', fill)
        receipt = self._action(desktop, 'default', 'saved_login', 'choose', request_id=state['signin']['id'],
                               request_revision=1, login_handle=signin.meta.id)
        assert receipt['status'] == 'denied'
        assert done.wait(10)
        thread.join(2)
        assert json.loads(result[0])['status'] == 'deferred'
        fill.assert_not_called()
