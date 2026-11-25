"""Regression tests: PromptCacheWarmer reads live agent client dynamically and refreshes once on 401."""

from __future__ import annotations

from types import SimpleNamespace
import time

from agent.prompt_cache_lifecycle import warmer as pcw


class _Auth401Error(Exception):
    status_code = 401


class _StubClient:
    def __init__(self, *, fail_401: bool = False):
        self.fail_401 = fail_401
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def with_options(self, **kwargs):
        return self

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_401:
            raise _Auth401Error("401 invalid bearer")
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, cache_read_input_tokens=50_000, cache_creation_input_tokens=0, output_tokens=1),
            stop_reason="end_turn",
        )


def _make_run(client):
    return pcw._Run(
        api_kwargs={"model": "claude-opus-4-6", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
        client=client,
        model="claude-opus-4-6",
        cached_tokens=50_000,
        uncached_tokens=10,
        ttl_s=300.0,
        deadline_at=time.time() + 300.0,
        is_current=lambda: True,
        api_mode="anthropic_messages",
        billing={"provider": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-opus-4-6",
                 "api_key": "", "session_id": "s1", "source": "cli", "reset_epoch": 0},
    )


def test_warmer_uses_live_rotated_anthropic_client_instead_of_frozen_snapshot():
    stale_client = _StubClient(fail_401=True)
    fresh_client = _StubClient(fail_401=False)
    agent = SimpleNamespace(_anthropic_client=fresh_client, provider="anthropic", model="claude-opus-4-6",
                            api_mode="anthropic_messages", _session_reset_epoch=0)
    warmer = pcw.PromptCacheWarmer(agent)
    run = _make_run(stale_client)

    msg = warmer._send(run)

    assert stale_client.calls == []
    assert len(fresh_client.calls) == 1
    assert run.client is fresh_client
    assert msg.stop_reason == "end_turn"


def test_warmer_refreshes_anthropic_client_once_on_401_and_retries():
    stale_client = _StubClient(fail_401=True)
    fresh_client = _StubClient(fail_401=False)
    refresh_calls = 0

    def _refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        agent._anthropic_client = fresh_client
        return True

    agent = SimpleNamespace(
        _anthropic_client=stale_client,
        _try_refresh_anthropic_client_credentials=_refresh,
        provider="anthropic",
        model="claude-opus-4-6",
        api_mode="anthropic_messages",
        _session_reset_epoch=0,
    )
    warmer = pcw.PromptCacheWarmer(agent)
    run = _make_run(stale_client)

    msg = warmer._send(run)

    assert refresh_calls == 1
    assert len(stale_client.calls) == 1
    assert len(fresh_client.calls) == 1
    assert run.client is fresh_client
    assert msg.stop_reason == "end_turn"


def test_warmer_does_not_loop_when_refreshed_client_also_401s():
    stale_client = _StubClient(fail_401=True)
    still_bad = _StubClient(fail_401=True)
    refresh_calls = 0

    def _refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        agent._anthropic_client = still_bad
        return True

    agent = SimpleNamespace(
        _anthropic_client=stale_client,
        _try_refresh_anthropic_client_credentials=_refresh,
        provider="anthropic",
        model="claude-opus-4-6",
        api_mode="anthropic_messages",
        _session_reset_epoch=0,
    )
    warmer = pcw.PromptCacheWarmer(agent)
    run = _make_run(stale_client)

    try:
        warmer._send(run)
        raise AssertionError("expected _Auth401Error")
    except _Auth401Error:
        pass
    assert refresh_calls == 1
    assert len(stale_client.calls) == 1
    assert len(still_bad.calls) == 1
