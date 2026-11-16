"""Grok mobile usage contracts with synthetic credentials and mocked HTTP."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import account_usage as usage
from plugins.cloudseed_mobile import provider_accounts as accounts


BILLING_URL = 'https://cli-chat-proxy.grok.com/v1/billing?format=credits'
PERIOD_END = '2030-01-08T00:00:00Z'


def saved_token(subject):
    payload = base64.urlsafe_b64encode(json.dumps({'sub': subject}).encode()).decode().rstrip('=')
    return f'fixture.{payload}.not-a-signature'


@pytest.fixture
def grok_entry():
    return SimpleNamespace(access_token=saved_token('user-a'), base_url='https://api.x.ai/v1')


def billing_payload(percent):
    return {'config': {'creditUsagePercent': percent, 'currentPeriod': {
        'type': 'USAGE_PERIOD_TYPE_WEEKLY',
        'start': '2030-01-01T00:00:00Z', 'end': PERIOD_END,
    }}}


@pytest.mark.parametrize('base_url', ['https://api.x.ai/v1', 'https://api.x.ai/v1/', ''])
@pytest.mark.parametrize('percent', [0, 0.5, 42.75, 100])
def test_grok_probe_maps_used_weekly_percent(monkeypatch, grok_entry, base_url, percent):
    grok_entry.base_url = base_url
    get_json = Mock(return_value=billing_payload(percent))
    monkeypatch.setattr(usage, '_get_json', get_json)

    result = accounts._probe_usage('xai-oauth', grok_entry)

    assert result['status'] == 'available'
    assert result['windows'] == [{'label': 'Current week', 'used_percent': percent,
                                 'remaining_percent': 100 - percent,
                                 'reset_at': PERIOD_END, 'detail': None}]
    get_json.assert_called_once_with(BILLING_URL, {
        'Authorization': f'Bearer {grok_entry.access_token}', 'X-XAI-Token-Auth': 'xai-grok-cli',
        'x-userid': 'user-a', 'x-grok-client-version': '1.0.45',
        'x-grok-client-mode': 'headless',
        'Accept': 'application/json', 'Content-Type': 'application/json',
    }, timeout=5.0)


def test_grok_probe_omitted_percent_on_healthy_reply_is_zero(monkeypatch, grok_entry):
    # xAI drops zero-valued fields, so an unused account's reply has a period but no percentage.
    payload = billing_payload(None)
    payload['config'].pop('creditUsagePercent')
    monkeypatch.setattr(usage, '_get_json', Mock(return_value=payload))

    window = accounts._probe_usage('xai-oauth', grok_entry)['windows'][0]

    assert window['used_percent'] == 0
    assert window['remaining_percent'] == 100
    assert window['reset_at'] == PERIOD_END


@pytest.mark.parametrize('payload', [{}, {'config': {}}, {'config': {'currentPeriod': {}}},
                                     {'config': {'currentPeriod': {'type': 'USAGE_PERIOD_TYPE_WEEKLY'}}}],
                         ids=['no-config', 'empty-config', 'empty-period', 'period-without-end'])
def test_grok_probe_omitted_percent_without_period_stays_unknown(monkeypatch, grok_entry, payload):
    monkeypatch.setattr(usage, '_get_json', Mock(return_value=payload))

    window = accounts._probe_usage('xai-oauth', grok_entry)['windows'][0]

    assert window['used_percent'] is None
    assert window['remaining_percent'] is None


@pytest.mark.parametrize('percent', [None, float('nan'), float('inf'), -float('inf'), -1, 101, True, '25', {}],
                         ids=['null', 'nan', 'inf', 'negative-inf', 'negative', 'over-limit', 'bool', 'string', 'object'])
def test_grok_probe_unknown_percent_preserves_period(monkeypatch, grok_entry, percent):
    payload = billing_payload(percent)
    monkeypatch.setattr(usage, '_get_json', Mock(return_value=payload))

    window = accounts._probe_usage('xai-oauth', grok_entry)['windows'][0]

    assert window['label'] == 'Current week'
    assert window['used_percent'] is None
    assert window['remaining_percent'] is None
    assert window['reset_at'] == PERIOD_END


@pytest.mark.parametrize('period_type', ['USAGE_PERIOD_TYPE_MONTHLY', 'USAGE_PERIOD_TYPE_UNSPECIFIED', None])
def test_grok_probe_other_period_uses_generic_label(monkeypatch, grok_entry, period_type):
    payload = billing_payload(25)
    payload['config']['currentPeriod']['type'] = period_type
    monkeypatch.setattr(usage, '_get_json', Mock(return_value=payload))
    window = accounts._probe_usage('xai-oauth', grok_entry)['windows'][0]
    assert window['label'] == 'Current period'
    assert window['reset_at'] == PERIOD_END


@pytest.mark.parametrize('base_url', [
    'https://proxy.example.invalid/v1', 'https://api.x.ai.evil.invalid/v1',
    'http://api.x.ai/v1', 'https://api.x.ai/v2', 'https://chatgpt.com/backend-api/codex',
    'https://api.x.ai/v1?proxy=1', 'https://api.x.ai@evil.invalid/v1',
])
def test_grok_probe_refuses_other_routes(monkeypatch, grok_entry, base_url):
    grok_entry.base_url = base_url
    get_json = Mock()
    monkeypatch.setattr(usage, '_get_json', get_json)
    with pytest.raises(ValueError, match='^Unsupported usage route$'):
        accounts._probe_usage('xai-oauth', grok_entry)
    get_json.assert_not_called()


@pytest.mark.parametrize('token', [
    'opaque-fixture', 'fixture.e30.not-a-signature', 'fixture.!.not-a-signature',
    *[saved_token(subject) for subject in (None, '', ' ', 42, {}, [], 'a\r\nb', 'a\tb', 'é', 'x' * 1025)],
])
def test_grok_missing_identity_stays_unavailable_without_http(monkeypatch, tmp_path, caplog, token):
    import hermes_constants
    from agent import credential_pool
    from hermes_cli import auth

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: tmp_path)
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps({'credential_pool': {'xai-oauth': [
        {'id': 'local-row-not-a-user-id', 'source': 'manual', 'priority': 0,
         'auth_type': 'oauth', 'access_token': token},
    ]}}))
    original = path.read_bytes()
    forbidden = Mock(side_effect=AssertionError('No HTTP, pool load, resolver or refresh allowed'))
    monkeypatch.setattr(usage, '_get_json', forbidden)
    monkeypatch.setattr(credential_pool, 'load_pool', forbidden)
    monkeypatch.setattr(auth, 'resolve_xai_oauth_runtime_credentials', forbidden)
    monkeypatch.setattr(auth, 'refresh_xai_oauth_pure', forbidden)

    result = accounts.get_account_usage('xai-oauth', refresh=True)
    section = next(p for p in result['providers'] if p['id'] == 'xai-oauth')
    result_usage = section['accounts'][0]['usage']
    assert result_usage['status'] == 'unavailable'
    assert result_usage['windows'] == []
    assert result_usage['message'] == 'Usage is temporarily unavailable. Try again shortly.'
    assert token not in json.dumps(result) + caplog.text
    assert path.read_bytes() == original
    forbidden.assert_not_called()


@pytest.mark.parametrize('status_code', [200, 401, 403, 500])
def test_grok_usage_inventory_is_read_only_and_sanitized(monkeypatch, tmp_path, caplog, status_code):
    import httpx
    import hermes_constants
    from agent import credential_pool
    from hermes_cli import auth

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: tmp_path)
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps({'credential_pool': {'xai-oauth': [
        {'id': 'grok-a', 'label': 'Work Grok', 'source': 'manual', 'priority': 0,
         'auth_type': 'oauth', 'base_url': 'https://api.x.ai/v1', 'access_token': saved_token('user-a')},
        {'id': 'grok-b', 'label': 'Personal Grok', 'source': 'manual', 'priority': 1,
         'auth_type': 'oauth', 'base_url': 'https://api.x.ai/v1', 'access_token': saved_token('user-b')},
    ]}}))
    original = path.read_bytes()
    forbidden = Mock(side_effect=AssertionError('Usage must not resolve or refresh credentials'))
    monkeypatch.setattr(credential_pool, 'load_pool', forbidden)
    monkeypatch.setattr(auth, 'resolve_xai_oauth_runtime_credentials', forbidden)
    monkeypatch.setattr(auth, 'refresh_xai_oauth_pure', forbidden)
    seen = []

    def billing(request):
        seen.append(request)
        assert request.method == 'GET'
        assert str(request.url) == BILLING_URL
        subject = request.headers['x-userid']
        assert subject in {'user-a', 'user-b'}
        assert request.headers['Authorization'] == f'Bearer {saved_token(subject)}'
        assert request.headers['X-XAI-Token-Auth'] == 'xai-grok-cli'
        assert request.headers['x-grok-client-version'] == '1.0.45'
        assert request.headers['x-grok-client-mode'] == 'headless'
        percent = 25 if subject == 'user-a' else 75
        return httpx.Response(status_code, json=billing_payload(percent) if status_code == 200
                              else {'error': 'PRIVATE RESPONSE fixture-only-grok'})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(billing), **kwargs))
    inventory = accounts.get_accounts()
    section = next(p for p in inventory['providers'] if p['id'] == 'xai-oauth')
    assert section['display_name'] == 'Grok'
    assert section['accounts'][0]['label'] == 'Work Grok'
    assert section['accounts'][0]['usage']['status'] == 'unknown'
    assert not seen  # metadata never probes

    result = accounts.get_account_usage('xai-oauth', refresh=True)
    section = next(p for p in result['providers'] if p['id'] == 'xai-oauth')
    account_usage = section['accounts'][0]['usage']
    for account in section['accounts']:
        if status_code == 200:
            assert account['usage']['status'] == 'available'
            expected_percent = 25 if account['id'] == 'grok-a' else 75
            assert account['usage']['windows'][0]['used_percent'] == expected_percent
        else:
            assert account['usage']['status'] == 'unavailable'
            assert account['usage']['windows'] == []
    assert len(seen) == 2
    assert {request.headers['x-userid'] for request in seen} == {'user-a', 'user-b'}
    assert path.read_bytes() == original
    assert 'fixture-only-grok' not in json.dumps(result)
    assert 'PRIVATE RESPONSE' not in json.dumps(result)
    for subject in ('user-a', 'user-b'):
        assert subject not in json.dumps(result) + caplog.text
        assert saved_token(subject) not in json.dumps(result) + caplog.text
    forbidden.assert_not_called()
    accounts.get_account_usage('xai-oauth', refresh=True)
    assert len(seen) == 2  # retry fence applies to Grok, too
    cached = next(p for p in accounts.get_accounts()['providers'] if p['id'] == 'xai-oauth')
    assert cached['accounts'][0]['usage'] == account_usage



# "Use now" on the phone (handoff-use-now-button.md): each provider section says which account the
# router's manual override pins and until when, read where the router reads it.
def _pool_home(monkeypatch, tmp_path, *, strategy='smart', home=None):
    import hermes_constants
    from hermes_cli.config_backend import write_config_key
    home = home or tmp_path
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: tmp_path)
    write_config_key(home / 'config.yaml', 'credential_pool_strategies.openai-codex', strategy)
    if home == tmp_path:
        (home / 'auth.json').write_text(json.dumps({'credential_pool': {'openai-codex': [
            {'id': 'codex-a', 'label': 'A', 'source': 'manual', 'priority': 0, 'auth_type': 'oauth',
             'access_token': 'fixture-only-a'},
            {'id': 'codex-b', 'label': 'B', 'source': 'manual', 'priority': 1, 'auth_type': 'oauth',
             'access_token': 'fixture-only-b'},
        ]}}))
    return home


def _write_router_state(root, state):
    (root / 'state').mkdir(exist_ok=True)
    (root / 'state' / 'account_router.json').write_text(state if isinstance(state, str) else json.dumps(state))


def _codex(inventory):
    return next(p for p in inventory['providers'] if p['id'] == 'openai-codex')


def test_live_pin_is_shown_with_its_expiry(monkeypatch, tmp_path):
    import time
    from datetime import UTC, datetime
    home = _pool_home(monkeypatch, tmp_path)
    until = time.time() + 3600
    _write_router_state(home, {'version': 1, 'pins': {'openai-codex': {'entry_id': 'codex-b', 'until': until}}})
    section = _codex(accounts.get_accounts())
    assert section['pin'] == {'account_id': 'codex-b',
                              'until': datetime.fromtimestamp(until, UTC).isoformat().replace('+00:00', 'Z')}
    assert section['pin']['until'].endswith('Z')
    assert all(p['pin'] is None for p in accounts.get_accounts()['providers'] if p['id'] != 'openai-codex')


@pytest.mark.parametrize('pin', [
    {'entry_id': 'codex-b', 'until': 1},  # expired
    {'entry_id': 'codex-gone', 'until': 4_102_444_800},  # no longer in this pool
    {'entry_id': 'codex-b'}, {'entry_id': 'codex-b', 'until': 'tomorrow'}, {'entry_id': 'codex-b', 'until': True},
    {'entry_id': 'codex-b', 'until': 1e300}, {'entry_id': ['codex-b'], 'until': 4_102_444_800}, 'codex-b',
], ids=['expired', 'not-in-pool', 'no-until', 'text-until', 'bool-until', 'huge-until', 'list-id', 'not-an-object'])
def test_no_live_pin_reads_as_null(monkeypatch, tmp_path, pin):
    home = _pool_home(monkeypatch, tmp_path)
    _write_router_state(home, {'version': 1, 'pins': {'openai-codex': pin}})
    assert _codex(accounts.get_accounts())['pin'] is None


def test_a_pin_only_counts_for_a_smart_pool(monkeypatch, tmp_path):
    home = _pool_home(monkeypatch, tmp_path, strategy='fill_first')
    _write_router_state(home, {'version': 1, 'pins': {'openai-codex': {'entry_id': 'codex-b', 'until': 4_102_444_800}}})
    assert _codex(accounts.get_accounts())['pin'] is None


@pytest.mark.parametrize('state', ['{not json', '[]', json.dumps({'version': 2, 'pins': {}}),
                                   json.dumps({'version': 1, 'pins': []}), json.dumps({'version': 1, 'pins': 'x'})],
                         ids=['bad-json', 'not-an-object', 'other-version', 'pins-list', 'pins-text'])
def test_unreadable_router_state_is_no_pin_never_a_route_error(monkeypatch, tmp_path, state):
    home = _pool_home(monkeypatch, tmp_path)
    _write_router_state(home, state)
    inventory = accounts.get_accounts()
    assert inventory['ok'] is True and _codex(inventory)['pin'] is None
    assert [a['id'] for a in _codex(inventory)['accounts']] == ['codex-a', 'codex-b']


def test_use_now_returns_the_new_pin_in_the_same_response(monkeypatch, tmp_path):
    """set_primary pins under smart (even the account already first) and answers with the inventory."""
    _pool_home(monkeypatch, tmp_path)
    section = _codex(accounts.get_accounts())
    assert section['pin'] is None
    after = _codex(accounts.set_primary({'provider': 'openai-codex', 'account_id': 'codex-a',
                                         'profile_id': accounts.get_accounts()['profile_id'],
                                         'revision': section['revision']}))
    assert after['pin']['account_id'] == 'codex-a' and after['pin']['until'].endswith('Z')


def test_a_borrowed_pool_shows_the_pin_the_router_honours(monkeypatch, tmp_path):
    """A profile using the root's accounts reads the root's router state, as the router does for it."""
    _pool_home(monkeypatch, tmp_path)
    _write_router_state(tmp_path, {'version': 1, 'pins': {'openai-codex': {'entry_id': 'codex-b',
                                                                            'until': 4_102_444_800}}})
    _pool_home(monkeypatch, tmp_path, home=tmp_path / 'profiles' / 'b')
    section = _codex(accounts.get_accounts())
    assert section['scope'] == 'shared_root' and not section['can_set_primary']
    assert section['pin'] == {'account_id': 'codex-b', 'until': '2100-01-01T00:00:00Z'}
