"""A peer's OAuth refresh must not detach a 429 from the account in use."""

import time

import pytest

from agent.credential_persistence import fingerprint_secret_value
from agent.credential_pool import (
    EXHAUSTED_TTL_429_SECONDS, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS,
    PooledCredential, STATUS_EXHAUSTED, load_pool,
)
from agent.credential_pool_model_cooldowns import model_cooldown_until
from hermes_cli.auth import read_credential_pool, write_credential_pool
from tests.agent import test_fallback_429_after_timeout as pool_tests

pooled_agent = pool_tests.pooled_agent


def _peer_refresh(agent, pool, monkeypatch):
    """Persist a peer rotation, reload the pool, but keep the request's old bearer."""
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)
    rows = [entry.to_dict() for entry in pool.entries()]
    for row in rows:
        row.update(auth_type="oauth", source="manual:hermes_pkce",
                   refresh_token=f"fixture-refresh-{row['id']}",
                   expires_at_ms=int((time.time() + 86400) * 1000))
    write_credential_pool(pool.provider, rows)
    rows = read_credential_pool(pool.provider)
    rows[0].update(access_token="peer-refreshed-bearer", refresh_token="peer-refreshed-grant")
    write_credential_pool(pool.provider, rows)
    reloaded = load_pool(pool.provider)
    # A shared cursor is NOT request provenance, even when it points at a healthy row.
    reloaded.set_current("entry-1")
    agent._credential_pool = reloaded
    agent._credential_pool_entry_id = "entry-0"
    return reloaded


def _run_repeated_failure(agent, pool, monkeypatch, headers):
    calls, waits = [], []
    identities = {fingerprint_secret_value(entry.runtime_api_key): entry.id for entry in pool.entries()}
    identities[fingerprint_secret_value(agent.api_key)] = "entry-0"
    agent._api_max_retries = 2

    def api_call(_kwargs):
        entry_id = identities.get(fingerprint_secret_value(agent.api_key))
        calls.append(entry_id)
        if entry_id == "entry-0":
            if len(calls) > 10:
                return pool_tests._mock_response("finite escape bound")
            raise pool_tests._pool_error(headers=headers)
        return pool_tests._mock_response("healthy account answered")

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    monkeypatch.setattr(agent, "_try_activate_fallback", lambda **kwargs: False)
    monkeypatch.setattr(agent, "_try_recover_primary_transport", lambda *args, **kwargs: False)
    monkeypatch.setattr("agent.turn_api_error.interruptible_backoff_sleep",
                        lambda _agent, seconds, *_args, **_kwargs: waits.append(seconds))
    result = agent.run_conversation("hello")
    assert calls == ["entry-0", "entry-1"], (calls, waits)
    assert waits == []
    assert result["completed"] is True
    assert result["final_response"] == "healthy account answered"


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex", "xai-oauth"])
@pytest.mark.parametrize("hint,seconds", [
    ("reset", 7200), ("short", EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS),
    ("missing", EXHAUSTED_TTL_429_SECONDS),
])
def test_peer_refreshed_request_benches_stable_entry(pooled_agent, monkeypatch, provider, hint, seconds):
    agent, pool = pooled_agent(provider)
    pool = _peer_refresh(agent, pool, monkeypatch)
    now = time.time()
    headers = {"reset": {"Retry-After": "7200"}, "short": {"Retry-After": "0.01"}, "missing": {}}[hint]
    _run_repeated_failure(agent, pool, monkeypatch, headers)
    rows = read_credential_pool(provider)
    first = PooledCredential.from_dict(provider, next(row for row in rows if row["id"] == "entry-0"))
    until = model_cooldown_until(first, agent.model) if provider == "anthropic" else first.last_error_reset_at
    assert until is not None and until >= now + seconds
    assert fingerprint_secret_value(first.access_token) == fingerprint_secret_value("peer-refreshed-bearer")
    second = next(entry for entry in pool.entries() if entry.id == "entry-1")
    assert second.last_status != STATUS_EXHAUSTED
    assert model_cooldown_until(second, agent.model) is None
    if provider == "anthropic":
        assert model_cooldown_until(first, "sibling-model") is None


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex", "xai-oauth"])
def test_stale_id_still_benches_key_matched_request(pooled_agent, monkeypatch, provider):
    """A matched request key still outranks a stale id (#79156)."""
    agent, pool = pooled_agent(provider)
    agent._credential_pool_entry_id = "entry-1"
    _run_repeated_failure(agent, pool, monkeypatch, {"Retry-After": "600"})
    first, second = pool.entries()
    assert first.last_status == STATUS_EXHAUSTED or model_cooldown_until(first, agent.model) is not None
    assert second.last_status != STATUS_EXHAUSTED
    assert model_cooldown_until(second, agent.model) is None


def test_unknown_key_and_missing_id_do_not_bench_shared_cursor(pooled_agent):
    """Without either identity, a shared selection is not evidence against an account."""
    agent, pool = pooled_agent("anthropic")
    pool.set_current("entry-1")
    pool.mark_exhausted_and_rotate(status_code=429, api_key_hint="unknown-request-bearer",
                                   credential_id="removed-entry", model=agent.model)
    assert all(entry.last_status != STATUS_EXHAUSTED and not entry.model_cooldowns for entry in pool.entries())
