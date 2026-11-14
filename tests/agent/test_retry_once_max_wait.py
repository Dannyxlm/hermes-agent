"""Bound the last pool row's retry-once wait without changing default recovery."""

import time
from unittest.mock import MagicMock, patch

import pytest

from agent.credential_pool import STATUS_EXHAUSTED
from agent.credential_pool_model_cooldowns import model_cooldown_until
from hermes_cli.config import atomic_config_write, get_config_path
from tests.agent import test_fallback_429_after_timeout as pool_tests

pooled_agent = pool_tests.pooled_agent


def _set_policy(value):
    fallback = {} if value is None else {"retry_once_max_wait_seconds": value}
    atomic_config_write(get_config_path(), {"fallback": fallback})


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex", "xai-oauth"])
@pytest.mark.parametrize("policy,body_delay,retry_after,switches", [
    (120, 7200, 30, True),    # Body reset wins even with a short Retry-After.
    (120, None, 121, True),   # Retry-After alone supplies the reset hint.
    (120, None, 30, False),
    (120, None, 120, False),  # Strictly further out, not equal.
    (120, 30, 7200, False),   # Longer rotation bench hint does not override reset precedence.
    (0, 7200, 600, False),
    (None, 7200, 600, False),
    (-1, 7200, 600, False),
    ("bad", 7200, 600, False),
    ("nan", 7200, 600, False),
    (120, None, None, False), # Unknown reset retains retry-once.
])
def test_last_usable_row_falls_back_only_past_opt_in_wait(
    pooled_agent, monkeypatch, provider, policy, body_delay, retry_after, switches,
):
    _set_policy(policy)
    agent, pool = pooled_agent(provider, other="exhausted")
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    original_model = agent.model
    agent._fallback_chain = [{
        "provider": "zai", "model": "fallback-model", "base_url": "https://fallback.example/v1",
    }]
    body = {"error": {"message": pool_tests._SUBSCRIPTION_LIMIT}}
    if body_delay is not None:
        body["error"]["reset_at"] = now + body_delay
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    error = pool_tests._pool_error(headers=headers, body=body)
    calls, waits = [], []

    def api_call(_kwargs):
        calls.append((agent.provider, agent.model))
        if len(calls) == 1:
            raise error
        return pool_tests._mock_response("recovered")

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    monkeypatch.setattr("agent.turn_api_error.interruptible_backoff_sleep",
                        lambda _agent, seconds, *_args, **_kwargs: waits.append(seconds))
    client = MagicMock()
    client.api_key = "fb-key"
    client.base_url = "https://fallback.example/v1"
    client._custom_headers = client.default_headers = None
    with (
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, "fallback-model")),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
    ):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert result["final_response"] == "recovered"
    assert agent._fallback_activated is switches
    assert calls == [(provider, original_model),
                     ("zai", "fallback-model") if switches else (provider, original_model)]
    first = pool.entries()[0]
    if switches:
        assert waits == []
        assert not pool.has_available(model=original_model)
        reset = now + (body_delay if body_delay is not None else retry_after)
        if provider == "anthropic":
            assert model_cooldown_until(first, original_model) >= reset
            assert first.last_status != STATUS_EXHAUSTED
            assert pool.has_available(model="sibling-model")
        else:
            assert first.last_status == STATUS_EXHAUSTED
            assert first.last_error_reset_at >= reset
    else:
        assert len(waits) == 1
        if retry_after is not None:
            assert waits == [min(retry_after, 600)]
        assert first.last_status != STATUS_EXHAUSTED
        assert model_cooldown_until(first, original_model) is None


def test_wait_policy_hot_reload_and_profile_scope(pooled_agent, monkeypatch, tmp_path):
    _set_policy(0)
    agent, pool = pooled_agent("anthropic", other="exhausted")
    reset = time.time() + 7200

    def recover():
        return agent._recover_with_credential_pool(
            status_code=429, has_retried_429=False, error_context={"reset_at": reset},
        )

    assert recover() == (False, True)
    assert pool.has_available(model=agent.model)
    # A profile's own policy must not inherit the root process's opt-in.
    root_home = get_config_path().parent
    _set_policy(120)
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    _set_policy(0)
    assert recover() == (False, True)
    assert pool.has_available(model=agent.model)
    monkeypatch.setenv("HERMES_HOME", str(root_home))
    assert recover() == (False, True)  # No alternate, but now the row is benched.
    assert not pool.has_available(model=agent.model)
    assert model_cooldown_until(pool.entries()[0], agent.model) == reset
