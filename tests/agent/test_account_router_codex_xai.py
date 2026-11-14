"""Subscription meters use only the supplied saved grant, never a resolver."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
import base64
import json

import httpx
import pytest

from agent import account_usage as usage
from agent.account_router import meters_codex as codex, meters_xai as xai
from agent.account_router.types import MeterWindow

NOW = 1_900_000_000.0
END = "2030-01-08T00:00:00Z"
END_EPOCH = datetime.fromisoformat(END).timestamp()
BILLING = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
USER = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"


def saved_entry(provider, **claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return SimpleNamespace(
        provider=provider, id="row-a", label="fixture-account", base_url=None,
        access_token=f"fixture.{payload}.not-a-signature",
    )


@pytest.fixture(autouse=True)
def no_credential_resolution(monkeypatch):
    from agent import credential_pool

    def forbidden(*args, **kwargs):
        pytest.fail("Meter tried to resolve or refresh credentials")

    monkeypatch.setattr(credential_pool, "load_pool", forbidden)
    monkeypatch.setattr(usage, "_resolve_codex_usage_credentials", forbidden)
    monkeypatch.setattr(usage, "resolve_codex_runtime_credentials", forbidden)


@pytest.fixture
def codex_entry():
    return saved_entry("openai-codex", **{"https://api.openai.com/auth": {
        "chatgpt_account_id": "workspace-a", "chatgpt_plan_type": "prolite",
        "chatgpt_data_residency": "eu",
    }})


@pytest.fixture
def xai_entry():
    return saved_entry("xai-oauth", sub="user-a", tier=2)


def billing(percent=25):
    return {"config": {"creditUsagePercent": percent, "currentPeriod": {
        "type": "USAGE_PERIOD_TYPE_WEEKLY", "end": END,
    }}}


def test_codex_fetch_duration_not_position_saved_grant_and_extras(monkeypatch, codex_entry):
    payload = {"plan_type": "pro", "rate_limit": {"primary_window": {
        "limit_window_seconds": 604800, "used_percent": 44, "reset_at": NOW + 7200,
    }}, "rate_limit_reset_credits": {"available_count": 3},
        "credits": {"has_credits": True, "balance": "57155.5"}}
    get_json = Mock(return_value=payload)
    monkeypatch.setattr(usage, "_get_json", get_json)
    result = codex.fetch(codex_entry)
    assert (result.provider, result.entry_id, result.plan, result.source) == (
        "openai-codex", "row-a", "pro", "usage_api")
    assert result.windows == (MeterWindow("weekly", 44, NOW + 7200),)
    assert result.extras == {"banked_resets": 3, "credits": 57155.5}
    url, headers = get_json.call_args.args
    assert url == "https://chatgpt.com/backend-api/wham/usage"
    assert headers["Authorization"] == f"Bearer {codex_entry.access_token}"
    assert headers["ChatGPT-Account-ID"] == "workspace-a"
    assert headers["x-openai-internal-codex-residency"] == "eu"
    assert get_json.call_args.kwargs == {"timeout": 5.0}
    assert result.fetched_at > 0


def test_codex_session_and_unrecognized_duration(monkeypatch, codex_entry):
    monkeypatch.setattr(usage, "_get_json", Mock(return_value={"rate_limit": {
        "primary_window": {"used_percent": 12, "reset_at": NOW, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 30, "reset_at": NOW, "limit_window_seconds": 2592000},
    }}))
    result = codex.fetch(codex_entry)
    assert result.plan == "prolite"  # payload wins; JWT is only a best-effort fallback.
    assert result.windows == (MeterWindow("session", 12, NOW), MeterWindow("period", 30, NOW))
    assert result.extras == {"banked_resets": 0, "credits": None}


@pytest.mark.parametrize("bad", [None, True, "25", -1, 101, float("nan"), float("inf"), {}])
def test_codex_invalid_percent_is_unknown_not_free(monkeypatch, codex_entry, bad):
    monkeypatch.setattr(usage, "_get_json", Mock(return_value={"rate_limit": {
        "secondary_window": {"used_percent": bad, "reset_at": NOW},
    }}))
    assert codex.fetch(codex_entry).windows == (MeterWindow("weekly", None, NOW),)


def test_codex_empty_payload_is_unknown(monkeypatch, codex_entry):
    monkeypatch.setattr(usage, "_get_json", Mock(return_value={}))
    assert codex.fetch(codex_entry).windows == ()


@pytest.mark.parametrize("balance,has_credits,expected", [
    ("0", True, 0.0), (123, False, 0.0), ("NaN", True, None),
    ("Infinity", True, None), (-1, True, None), (True, True, None),
])
def test_codex_credits_are_finite_or_unknown(monkeypatch, codex_entry, balance, has_credits, expected):
    monkeypatch.setattr(usage, "_get_json", Mock(return_value={
        "credits": {"has_credits": has_credits, "balance": balance},
    }))
    assert codex.fetch(codex_entry).extras["credits"] == expected


def test_xai_saved_identity_headers_plan_and_weekly_window(monkeypatch, xai_entry):
    get_json = Mock(side_effect=[billing(1.5), {"subscriptionTier": "GrokPro", "email": "ignored"}])
    monkeypatch.setattr(usage, "_get_json", get_json)
    result = xai.fetch(xai_entry)
    assert (result.provider, result.entry_id, result.plan, result.source) == (
        "xai-oauth", "row-a", "GrokPro", "usage_api")
    assert result.windows == (MeterWindow("weekly", 1.5, END_EPOCH),)
    assert result.extras == {}
    assert [call.args[0] for call in get_json.call_args_list] == [BILLING, USER]
    for call in get_json.call_args_list:
        assert call.kwargs == {"timeout": 5.0}
        headers = call.args[1]
        assert headers == {
            "Authorization": f"Bearer {xai_entry.access_token}", "X-XAI-Token-Auth": "xai-grok-cli",
            "x-userid": "user-a", "x-grok-client-version": "1.0.45", "x-grok-client-mode": "headless",
            "Accept": "application/json", "Content-Type": "application/json",
        }


@pytest.mark.parametrize("period_type,kind", [
    ("USAGE_PERIOD_TYPE_WEEKLY", "weekly"), ("USAGE_PERIOD_TYPE_MONTHLY", "period"), (None, "period"),
])
def test_xai_proto3_omitted_zero_requires_valid_period(monkeypatch, xai_entry, period_type, kind):
    payload = billing()
    del payload["config"]["creditUsagePercent"]
    payload["config"]["currentPeriod"]["type"] = period_type
    monkeypatch.setattr(usage, "_get_json", Mock(side_effect=[payload, {"subscriptionTier": "XPremiumPlus"}]))
    result = xai.fetch(xai_entry)
    assert result.plan == "XPremiumPlus"
    assert result.windows == (MeterWindow(kind, 0, END_EPOCH),)


@pytest.mark.parametrize("end", [None, "", "not-a-date", {}, True, float("inf")])
def test_xai_omitted_zero_without_valid_end_is_unknown(monkeypatch, xai_entry, end):
    payload = billing()
    del payload["config"]["creditUsagePercent"]
    payload["config"]["currentPeriod"]["end"] = end
    monkeypatch.setattr(usage, "_get_json", Mock(side_effect=[payload, {}]))
    assert xai.fetch(xai_entry).windows == (MeterWindow("weekly", None, None),)


@pytest.mark.parametrize("bad", [None, True, "25", -1, 101, float("nan"), float("inf"), {}])
def test_xai_invalid_explicit_percent_is_unknown(monkeypatch, xai_entry, bad):
    monkeypatch.setattr(usage, "_get_json", Mock(side_effect=[billing(bad), {}]))
    assert xai.fetch(xai_entry).windows == (MeterWindow("weekly", None, END_EPOCH),)


@pytest.mark.parametrize("subject", [None, "", " ", 42, {}, [], "a\r\nb", "é", "x" * 1025])
def test_xai_missing_identity_never_calls_http(monkeypatch, subject):
    get_json = Mock()
    monkeypatch.setattr(usage, "_get_json", get_json)
    with pytest.raises(ValueError, match="No usable saved Grok identity"):
        xai.fetch(saved_entry("xai-oauth", sub=subject))
    get_json.assert_not_called()


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
@pytest.mark.parametrize("route", ["https://proxy.invalid/v1", "https://api.x.ai.evil.invalid/v1", "http://api.x.ai/v1"])
def test_non_official_routes_never_send_tokens(monkeypatch, provider, route):
    entry = saved_entry(provider, sub="user-a")
    entry.base_url = route
    get_json = Mock()
    monkeypatch.setattr(usage, "_get_json", get_json)
    with pytest.raises(ValueError, match="Unsupported usage route"):
        (codex if provider == "openai-codex" else xai).fetch(entry)
    get_json.assert_not_called()


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_propagate_without_retry_refresh_or_output(monkeypatch, caplog, capsys, provider, status):
    entry = saved_entry(provider, sub="user-a")
    response = httpx.Response(status, request=httpx.Request("GET", "https://example.invalid"),
                              json={"private": "body-must-not-be-printed"})
    get_json = Mock(side_effect=httpx.HTTPStatusError("probe failed", request=response.request, response=response))
    monkeypatch.setattr(usage, "_get_json", get_json)
    with pytest.raises(httpx.HTTPStatusError):
        (codex if provider == "openai-codex" else xai).fetch(entry)
    assert get_json.call_count == 1
    assert not caplog.text
    assert capsys.readouterr() == ("", "")


def test_codex_headers_case_insensitive_duration_and_absolute_reset():
    headers = {"X-Codex-Primary-Used-Percent": "44.5", "X-Codex-Primary-Window-Minutes": "10080",
               "X-Codex-Primary-Reset-At": str(NOW + 7200),
               "X-Codex-Primary-Reset-After-Seconds": "3600"}
    assert codex.parse_headers(headers, now=NOW) == (MeterWindow("weekly", 44.5, NOW + 7200),)


def test_codex_headers_relative_reset_and_short_window():
    headers = {"x-codex-primary-used-percent": "0", "x-codex-primary-window-minutes": "300",
               "x-codex-primary-reset-after-seconds": "90",
               "x-codex-secondary-used-percent": "10", "x-codex-secondary-window-minutes": "43200"}
    assert codex.parse_headers(headers, now=NOW) == (
        MeterWindow("session", 0, NOW + 90), MeterWindow("period", 10, None))


@pytest.mark.parametrize("bad", ["nan", "inf", "-1", "101", "oops"])
def test_codex_invalid_header_percent_never_looks_unused(bad):
    assert codex.parse_headers({"x-codex-primary-used-percent": bad}, now=NOW) == (
        MeterWindow("session", None, None),)


def test_generic_rate_limit_headers_are_not_subscription_quota():
    assert codex.parse_headers({"x-ratelimit-limit-tokens": "1000", "x-ratelimit-remaining-tokens": "500"}) == ()
