"""Tokenless privacy refresh crosses the authenticated dispatcher without attaching runtimes."""
import pytest

from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer, rpc, open_bot
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service, registration
from tests.tui_gateway.test_mobile_session_parity import ordinary as ordinary
from tui_gateway import server


@pytest.mark.parametrize('surface', ['native', 'native_session'])
def test_tokenless_refresh_keeps_detached_scope_for_later_token_rotation(push_service, ordinary, peer, surface):
    peer.auth_identity = {'user_id': 'fixture-owner', 'provider': 'fixture-auth'}
    params = registration(open_bot()) if surface == 'native' else ordinary
    prefix = 'mobile.push' if surface == 'native' else 'mobile.session_push'
    original = rpc(prefix + '.register', **params, preview_enabled=True)['result']
    server._sessions.clear()
    refresh = {key: params[key] for key in ('installation_id', 'connection_id', 'environment')}
    other = 'mobile.session_push' if surface == 'native' else 'mobile.push'
    assert rpc(other + '.refresh', **refresh, categories=[])['result']['updated'] == 0
    peer.auth_identity = None
    assert rpc(prefix + '.refresh', **refresh, categories=[])['error']['code'] == 4403
    peer.auth_identity = {'user_id': 'fixture-owner', 'provider': 'fixture-auth'}
    narrowed = rpc(prefix + '.refresh', **refresh, categories=[], preview_enabled=False)['result']
    assert narrowed['subscriptions'] == [{'subscription_id': original['subscription_id'],
        'expires_at': original['expires_at'], 'categories': [], 'preview_enabled': False}]
    attempted_widen = rpc(prefix + '.refresh', **refresh, preview_enabled=True)['result']
    assert attempted_widen['subscriptions'] == narrowed['subscriptions']
    restored = rpc(prefix + '.refresh', **refresh, device_token='ef' * 32,
                   categories=['attention'], preview_enabled=True)['result']
    assert restored['subscriptions'][0]['subscription_id'] == original['subscription_id']
    assert restored['subscriptions'][0]['categories'] == ['attention']
    assert restored['subscriptions'][0]['preview_enabled'] is True
    assert not server._sessions
