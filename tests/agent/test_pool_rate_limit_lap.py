"""Review regressions: first-429 recovery has a finite free lap and separate bench hints."""

import time
from unittest.mock import patch

import pytest

from agent.agent_runtime_helpers import extract_api_error_context
from agent.credential_pool import (
    EXHAUSTED_TTL_429_SECONDS, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS,
    STATUS_EXHAUSTED, _normalize_error_context,
)
from agent.credential_pool_model_cooldowns import model_cooldown_until
from tests.agent import test_fallback_429_after_timeout as pool_tests
from tests.agent.test_fallback_429_after_timeout import _mock_response, _pool_error, _run_pool_retry

pooled_agent = pool_tests.pooled_agent


def _all_429_cycle(agent, monkeypatch, *, n, headers, round_trip):
    """Advance a fake clock instead of sleeping; return success at a hard escape bound."""
    agent._api_max_retries = 3
    calls, waits = [], []
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def api_call(_kwargs):
        calls.append(agent.api_key)
        if len(calls) > n + agent._api_max_retries + 1:
            return _mock_response("escaped at the test's finite call bound")
        clock[0] += round_trip
        raise _pool_error(headers=headers)

    def wait(_agent, seconds, *_args, **_kwargs):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    monkeypatch.setattr(agent, "_try_activate_fallback", lambda **kwargs: False)
    monkeypatch.setattr(agent, "_try_recover_primary_transport", lambda *args, **kwargs: False)
    monkeypatch.setattr("agent.turn_api_error.interruptible_backoff_sleep", wait)
    agent.run_conversation("hello")
    assert len(calls) <= n + agent._api_max_retries, (calls, waits)
    assert waits, (calls, waits)
    return calls


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex"])
@pytest.mark.parametrize("n", [2, 3])
@pytest.mark.parametrize("round_trip", [0.05, 61.0])
def test_all_429_short_hints_use_one_free_lap(pooled_agent, monkeypatch, provider, n, round_trip):
    agent, _pool = pooled_agent(provider, n=n)
    _all_429_cycle(agent, monkeypatch, n=n, round_trip=round_trip, headers={
        "x-ratelimit-reset-requests": "20ms", "x-ratelimit-reset-tokens": "30ms",
    })


@pytest.mark.parametrize("n", [2, 3])
def test_anthropic_retry_after_zero_does_not_spin_on_first_key(pooled_agent, monkeypatch, n):
    agent, _pool = pooled_agent("anthropic", n=n)
    calls = _all_429_cycle(agent, monkeypatch, n=n, round_trip=0.05, headers={"Retry-After": "0"})
    assert calls[:2] == ["pool-key-0", "pool-key-1"]


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex"])
def test_zero_wait_rotation_has_a_minimum_bench(pooled_agent, monkeypatch, provider):
    agent, pool = pooled_agent(provider)
    now = time.time()
    calls, waits = _run_pool_retry(agent, _pool_error(headers={"Retry-After": "0.01"}), monkeypatch)
    assert calls == ["pool-key-0", "pool-key-1"] and not waits
    first = pool.entries()[0]
    until = model_cooldown_until(first, agent.model) if provider == "anthropic" else first.last_error_reset_at
    assert until is not None and until >= now + EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS


@pytest.mark.parametrize("reset", [0, -1, 30])
def test_past_or_relative_epoch_is_not_an_exhaustion_hint(reset):
    assert "reset_at" not in _normalize_error_context({"reset_at": reset})


@pytest.mark.parametrize("hint", ["zero", "relative-epoch"])
def test_expired_model_hint_uses_default_ttl(pooled_agent, hint):
    agent, pool = pooled_agent("anthropic")
    now = time.time()
    reset = now if hint == "zero" else 30
    next_entry = pool.mark_exhausted_and_rotate(
        status_code=429, credential_id="entry-0", model=agent.model,
        error_context={"reset_at": reset},
    )
    assert next_entry is not None and next_entry.id == "entry-1"
    assert model_cooldown_until(pool.entries()[0], agent.model) >= now + EXHAUSTED_TTL_429_SECONDS


def test_model_rotation_rejects_same_entry_after_resync(pooled_agent):
    agent, pool = pooled_agent("anthropic")
    first = pool.entries()[0]
    with patch.object(pool, "_select_unlocked", return_value=(first, [])):
        result = pool.mark_exhausted_and_rotate(
            status_code=429, credential_id=first.id, model=agent.model,
            error_context={"reset_at": time.time() + 600},
        )
    assert result is None


def test_body_reset_precedence_and_longer_rotation_bench(monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    error = _pool_error(headers={"Retry-After": "7200"}, body={"error": {"reset_at": now + 600}})
    context = extract_api_error_context(error)
    assert context["reset_at"] == now + 600
    assert context.get("bench_until") == now + 7200


def test_retry_after_still_wins_for_reset_but_not_rotation_bench(monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    context = extract_api_error_context(_pool_error(headers={
        "Retry-After": "30", "x-ratelimit-reset-requests": "6m0s",
    }))
    assert context["reset_at"] == now + 30
    assert context.get("bench_until") == now + 360


def test_extract_unified_epoch_directly(monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    context = extract_api_error_context(_pool_error(headers={
        "anthropic-ratelimit-unified-reset": str(now + 7200),
    }))
    assert context["reset_at"] == now + 7200
    assert context.get("bench_until") == now + 7200


def test_stale_entry_id_does_not_bench_request_key_without_alternate(pooled_agent):
    agent, pool = pooled_agent("openai-codex", other="exhausted")
    agent._credential_pool_entry_id = "entry-1"
    recovered, retried = agent._recover_with_credential_pool(status_code=429, has_retried_429=False)
    assert recovered is False and retried is True
    assert pool.entries()[0].last_status != STATUS_EXHAUSTED
