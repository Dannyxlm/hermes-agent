"""Short-lived, exact-target CDP attachment for cross-process saved login."""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging

from tools.desktop_admission import assert_agent_admitted


DESKTOP_CDP = 'http://127.0.0.1:9225'


def published_target(task_id: str, origin: str) -> str:
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    from agent.vault_store import normalize_origin

    captured = SUPERVISOR_REGISTRY.capture(task_id)
    info = captured.call('Target.getTargetInfo', session_id=captured.page_session_id)['result']['targetInfo']
    if (info.get('type') != 'page' or not isinstance(info.get('targetId'), str) or not info['targetId']
            or normalize_origin(info.get('url', '')) != origin):
        raise ValueError('Sign-in page unavailable')
    return info['targetId']


class SigninPage:
    def __init__(self, socket, target_id: str):
        self.socket, self.target_id = socket, target_id
        self.cdp_url = DESKTOP_CDP
        self._page_session_id = None
        self._next_id = 0

    def call(self, method, params=None, *, page=False):
        self._next_id += 1
        request = {'id': self._next_id, 'method': method, 'params': params or {}}
        if page:
            request['sessionId'] = self._page_session_id
        self.socket.send(json.dumps(request))
        while True:
            reply = json.loads(self.socket.recv(timeout=7))
            if reply.get('id') == self._next_id:
                if 'error' in reply:
                    raise RuntimeError('Sign-in CDP request refused')
                return reply['result']

    def evaluate_runtime(self, expression):
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                               'awaitPromise': True}, page=True)
        if result.get('exceptionDetails'):
            return {'ok': False, 'error': 'Sign-in page evaluation refused'}
        return {'ok': True, 'result': result.get('result', {}).get('value')}


@contextmanager
def signin_page(target_id: str, origin: str):
    from tools.browser_tool_cdp import _resolve_cdp_override
    from websockets.sync.client import connect
    from agent.proxy_bypass import loopback_connect_kwargs
    from agent.vault_store import normalize_origin

    # This is the shared Desktop gate, never the producer's process-local default browser.
    assert_agent_admitted(DESKTOP_CDP)
    endpoint = _resolve_cdp_override(DESKTOP_CDP)
    # websockets DEBUG tracing includes payloads; never trace credential-bearing frames.
    silent = logging.Logger(__name__ + '.secret_socket', level=logging.CRITICAL)
    with connect(endpoint, open_timeout=7, max_size=1048576, logger=silent,
                 **loopback_connect_kwargs(endpoint)) as socket:
        page = SigninPage(socket, target_id)
        info = page.call('Target.getTargetInfo', {'targetId': target_id})['targetInfo']
        if info.get('targetId') != target_id or info.get('type') != 'page' or normalize_origin(info.get('url', '')) != origin:
            raise ValueError('Sign-in page changed')
        page._page_session_id = page.call('Target.attachToTarget', {'targetId': target_id, 'flatten': True})['sessionId']
        yield page
        # Closing the dedicated CDP connection detaches its page session even on failed fills.
