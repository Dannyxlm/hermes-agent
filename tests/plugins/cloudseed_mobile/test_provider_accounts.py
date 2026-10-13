"""Grok mobile usage contracts with synthetic credentials and mocked HTTP."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import account_usage as usage
from plugins.cloudseed_mobile import provider_accounts as accounts


BILLING_URL = 'https://cli-chat-proxy.grok.com/v1/billing?format=credits'
PERIOD_END = '2030-01-08T00:00:00Z'


@pytest.fixture
def grok_entry():
    return SimpleNamespace(access_token='fixture-only-grok', base_url='https://api.x.ai/v1')


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
        'Authorization': 'Bearer fixture-only-grok', 'X-XAI-Token-Auth': 'xai-grok-cli',
        'Accept': 'application/json', 'Content-Type': 'application/json',
    }, timeout=5.0)


@pytest.mark.parametrize('percent', [None, float('nan'), float('inf'), -float('inf'), -1, 101, True, '25', {}],
                         ids=['missing', 'nan', 'inf', 'negative-inf', 'negative', 'over-limit', 'bool', 'string', 'object'])
def test_grok_probe_unknown_percent_preserves_period(monkeypatch, grok_entry, percent):
    payload = billing_payload(percent)
    if percent is None:
        payload['config'].pop('creditUsagePercent')
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


@pytest.mark.parametrize('status_code', [200, 401, 403, 500])
def test_grok_usage_inventory_is_read_only_and_sanitized(monkeypatch, tmp_path, status_code):
    import httpx
    import hermes_constants
    from agent import credential_pool
    from hermes_cli import auth

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: tmp_path)
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps({'credential_pool': {'xai-oauth': [
        {'id': 'grok-a', 'label': 'Work Grok', 'source': 'manual', 'priority': 0,
         'auth_type': 'oauth', 'base_url': 'https://api.x.ai/v1', 'access_token': 'fixture-only-grok'},
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
        assert request.headers['Authorization'] == 'Bearer fixture-only-grok'
        assert request.headers['X-XAI-Token-Auth'] == 'xai-grok-cli'
        return httpx.Response(status_code, json=billing_payload(25) if status_code == 200
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
    if status_code == 200:
        assert account_usage['status'] == 'available'
        assert account_usage['windows'][0]['used_percent'] == 25
    else:
        assert account_usage['status'] == 'unavailable'
        assert account_usage['windows'] == []
    assert len(seen) == 1
    assert path.read_bytes() == original
    assert 'fixture-only-grok' not in json.dumps(result)
    assert 'PRIVATE RESPONSE' not in json.dumps(result)
    forbidden.assert_not_called()
    accounts.get_account_usage('xai-oauth', refresh=True)
    assert len(seen) == 1  # retry fence applies to Grok, too
    cached = next(p for p in accounts.get_accounts()['providers'] if p['id'] == 'xai-oauth')
    assert cached['accounts'][0]['usage'] == account_usage
