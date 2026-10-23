"""Regression test for #32646: fallback_providers not activated when
HTTP 429 follows a successful primary-transport recovery.

Reproduces the (timeout x N -> recover -> 429 -> no fallback) sequence
reported against zai/glm-5.1 -> zai/glm-4.7 on the Telegram gateway.

Scenario:
  1. ``_try_recover_primary_transport()`` succeeds after 3 timeouts and
     resets ``retry_count = 0`` so the rebuilt primary client gets one
     more attempt.
  2. The next attempt hits HTTP 429.
  3. Before this fix, an eager-fallback attempt that lost its race with
     a concurrent session mutating the on-disk credential pool could
     leave ``_fallback_index`` advanced past the chain length without
     setting ``_fallback_activated`` to True.  The subsequent 429s then
     short-circuited the eager-fallback gate (``_fallback_index >=
     len(_fallback_chain)``), so the retry budget burned on the primary
     model with no fallback ever attempted.
  4. The fix resets ``_fallback_index`` / ``_fallback_activated`` /
     ``TurnRetryState.has_retried_429`` after transport recovery so the post-recovery
     429 always gets a fresh fallback-chain attempt.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.credential_pool import (
    EXHAUSTED_TTL_429_SECONDS, STATUS_EXHAUSTED, CredentialPool, PooledCredential,
)
from agent.credential_pool_model_cooldowns import model_cooldown_until
from run_agent import AIAgent

def _make_tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

def _make_agent_with_fallback(fb_chain):
    """Build a minimal AIAgent with the given fallback chain configured."""
    with (
        patch("model_tools.get_tool_definitions", return_value=_make_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            provider="zai",
            model="glm-5.1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fb_chain,
        )
        agent.client = MagicMock()
        return agent

def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)

class ReadTimeout(Exception):
    pass

class RateLimitError(Exception):
    status_code = 429

    def __init__(self):
        super().__init__("Error code: 429 - rate limit exceeded")
        self.response = SimpleNamespace(headers={})
        self.body = {"error": {"message": "rate limit exceeded"}}

# Regression: post-recovery reset of fallback-chain state

class TestFallbackChainResetOnTransportRecovery:
    """The bug surfaced when a stale ``_fallback_index`` survived the
    transport-recovery cycle.  These tests exercise the reset directly
    via the same call sequence the conversation loop performs, without
    needing to drive the full ``run_conversation`` loop."""

    def test_run_conversation_fallbacks_on_429_after_timeout_recovery(self):
        """Full loop regression for #32646.

        Start the turn with the fallback chain already burned, matching
        the stale state reported in the issue. Two transient timeouts
        exhaust the retry loop and trigger primary transport recovery.
        The next primary attempt returns 429. The conversation loop must
        reset the stale fallback-chain state during recovery so that the
        post-recovery 429 activates the configured fallback provider.
        """
        fb_chain = [
            {
                "provider": "zai",
                "model": "glm-4.7",
                "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            }
        ]
        agent = _make_agent_with_fallback(fb_chain)
        agent._api_max_retries = 2

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            attempt = len(calls)
            if attempt == 1:
                agent._fallback_index = len(agent._fallback_chain)
                agent._fallback_activated = False
            if attempt <= 2:
                raise ReadTimeout("read timed out")
            if attempt == 3:
                raise RateLimitError()
            return _mock_response("Recovered via fallback")

        mock_fb_client = MagicMock()
        mock_fb_client.api_key = "primary-key-abcdef12"
        mock_fb_client.base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
        mock_fb_client._custom_headers = None
        mock_fb_client.default_headers = None

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(mock_fb_client, "glm-4.7"),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("hello")

        assert result["completed"] is True
        assert result["final_response"] == "Recovered via fallback"
        assert calls == [
            ("zai", "glm-5.1"),
            ("zai", "glm-5.1"),
            ("zai", "glm-5.1"),
            ("zai", "glm-4.7"),
        ]
        mock_resolve.assert_called_once()
        assert agent._fallback_activated is True
        assert agent.model == "glm-4.7"

# Defensive: pure-timeout cycle without 429 still works


_SUBSCRIPTION_LIMIT = "This request would exceed your account's rate limit"


@pytest.fixture
def pooled_agent(monkeypatch):
    """Real pool, recovery and credential swap; only the transport is replaced."""
    def build(provider, *, other="healthy", n=2):
        base_url = "https://pool.example/v1"
        model = "pool-model"
        entries = [PooledCredential(
            provider=provider, id=f"entry-{i}", label=f"account-{i}", priority=i,
            auth_type="api_key", source="manual", access_token=f"pool-key-{i}",
            base_url=base_url,
        ) for i in range(1 if other == "absent" else n)]
        overrides = {
            "duplicate": {"access_token": entries[0].access_token},
            "exhausted": {"last_status": STATUS_EXHAUSTED, "last_status_at": time.time(),
                          "last_error_code": 429, "last_error_reset_at": time.time() + 7200},
            "model-cooldown": {"model_cooldowns": {model: time.time() + 7200}},
            "sibling-model-cooldown": {"model_cooldowns": {"other-model": time.time() + 7200}},
            "other-endpoint": {"base_url": "https://elsewhere.example/v1"},
        }
        for name, value in overrides.get(other, {}).items():
            setattr(entries[1], name, value)
        pool = CredentialPool(provider=provider, entries=entries)
        first = pool.select(model=model)
        assert first is not None
        with (
            patch("model_tools.get_tool_definitions", return_value=_make_tool_defs()),
            patch("model_tools.check_toolset_requirements", return_value={}),
            patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
        ):
            agent = AIAgent(
                api_key=first.runtime_api_key, base_url=base_url, provider=provider,
                model=model, credential_pool=pool, api_mode="chat_completions",
                quiet_mode=True, skip_context_files=True, skip_memory=True,
                fallback_model=[],
            )
        monkeypatch.setattr(agent, "_replace_primary_openai_client", MagicMock())
        monkeypatch.setattr(agent, "_persist_session", MagicMock())
        monkeypatch.setattr(agent, "_save_trajectory", MagicMock())
        monkeypatch.setattr(agent, "_cleanup_task_resources", MagicMock())
        return agent, pool
    return build


def _pool_error(status=429, *, headers=None, body=None):
    error = RateLimitError()
    error.status_code = status
    error.args = (_SUBSCRIPTION_LIMIT if status == 429 else "origin response timeout",)
    error.response = SimpleNamespace(headers=headers or {})
    error.body = {"error": {"message": _SUBSCRIPTION_LIMIT}} if body is None else body
    return error


def _run_pool_retry(agent, error, monkeypatch):
    calls, waits = [], []

    def api_call(_kwargs):
        calls.append(agent.api_key)
        if len(calls) == 1:
            raise error
        return _mock_response("pool recovered")

    def backoff(_agent, wait_time, *_args, **_kwargs):
        waits.append(wait_time)

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    monkeypatch.setattr("agent.turn_api_error.interruptible_backoff_sleep", backoff)
    result = agent.run_conversation("hello")
    assert result["completed"] is True
    assert result["final_response"] == "pool recovered"
    return calls, waits


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex", "xai-oauth"])
@pytest.mark.parametrize("hint", [
    "retry-after", "over-cap", "body-reset", "vendor-reset", "unified-reset", "missing",
    "unified-with-retry-after", "body-with-longer-retry-after", "vendor-buckets",
])
def test_first_429_rotates_without_wait_and_preserves_reset(pooled_agent, monkeypatch, provider, hint):
    agent, pool = pooled_agent(provider)
    now = time.time()
    headers, reset, seconds = {
        "retry-after": ({"Retry-After": "600"}, {}, 600),
        "over-cap": ({"Retry-After": "7200"}, {}, 7200),
        "body-reset": ({}, {"reset_at": now + 7200}, 7200),
        "vendor-reset": ({"x-ratelimit-reset-tokens": "2h"}, {}, 7200),
        "unified-reset": ({"anthropic-ratelimit-unified-reset": str(now + 7200)}, {}, 7200),
        "missing": ({}, {}, EXHAUSTED_TTL_429_SECONDS),
        "unified-with-retry-after": (
            {"Retry-After": "600", "anthropic-ratelimit-unified-reset": str(now + 7200)}, {}, 7200,
        ),
        "body-with-longer-retry-after": ({"Retry-After": "7200"}, {"reset_at": now + 600}, 7200),
        "vendor-buckets": ({"x-ratelimit-reset-requests": "10s", "x-ratelimit-reset-tokens": "2h"}, {}, 7200),
    }[hint]
    body = {"error": {"message": _SUBSCRIPTION_LIMIT, **reset}}
    calls, waits = _run_pool_retry(agent, _pool_error(headers=headers, body=body), monkeypatch)
    assert calls == ["pool-key-0", "pool-key-1"]
    assert sum(waits) < 5
    benched = pool.entries()[0]
    if provider == "anthropic":
        # Preserve the existing per-model bench: sibling models remain usable.
        until = model_cooldown_until(benched, agent.model)
    else:
        assert benched.last_status == STATUS_EXHAUSTED
        until = benched.last_error_reset_at or benched.last_status_at + EXHAUSTED_TTL_429_SECONDS
    assert until >= now + seconds


@pytest.mark.parametrize("other", ["absent", "exhausted", "model-cooldown", "duplicate", "other-endpoint"])
@pytest.mark.parametrize("retry_after, expected", [(600, 600), (7200, 600)])
def test_first_429_without_usable_alternate_keeps_capped_wait(pooled_agent, monkeypatch, other, retry_after, expected):
    agent, pool = pooled_agent("anthropic", other=other)
    calls, waits = _run_pool_retry(agent, _pool_error(headers={"Retry-After": str(retry_after)}), monkeypatch)
    assert calls == ["pool-key-0", "pool-key-0"]
    assert waits == [expected]
    assert pool.entries()[0].last_status != STATUS_EXHAUSTED
    assert model_cooldown_until(pool.entries()[0], agent.model) is None


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex", "xai-oauth"])
def test_429_can_rotate_to_entry_cooled_only_for_another_model(pooled_agent, monkeypatch, provider):
    agent, _pool = pooled_agent(provider, other="sibling-model-cooldown")
    calls, waits = _run_pool_retry(agent, _pool_error(headers={"Retry-After": "600"}), monkeypatch)
    assert calls == ["pool-key-0", "pool-key-1"]
    assert sum(waits) < 5


def test_5xx_with_healthy_pool_still_honors_retry_after(pooled_agent, monkeypatch):
    agent, pool = pooled_agent("anthropic")
    error = _pool_error(524, headers={"Retry-After": "7200"}, body={"status": 524})
    calls, waits = _run_pool_retry(agent, error, monkeypatch)
    assert calls == ["pool-key-0", "pool-key-0"]
    assert waits == [600]
    assert all(entry.last_status != STATUS_EXHAUSTED for entry in pool.entries())
