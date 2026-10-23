"""Transport/snapshot recovery must keep credential provenance coherent after pool rotation."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from agent.credential_persistence import fingerprint_secret_value
from agent.credential_pool import STATUS_EXHAUSTED
from agent.credential_pool_model_cooldowns import model_cooldown_until
from agent.route_binding import reinstall_runtime_snapshot
from tests.agent import test_fallback_429_after_timeout as pool_tests

pooled_agent = pool_tests.pooled_agent


def _refresh_first_row(pool):
    first = pool.entries()[0]
    pool._replace_entry(first, replace(first, access_token="fixture-peer-first-v2"))
    pool._persist()


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
def test_transport_rebuild_keeps_rotated_account_in_conversation(pooled_agent, monkeypatch, provider):
    agent, pool = pooled_agent(provider, n=3)
    agent._api_max_retries = 1
    calls, waits, rebuilt_ids = [], [], []
    owners = {fingerprint_secret_value(entry.runtime_api_key): entry.id for entry in pool.entries()}
    owners[fingerprint_secret_value("fixture-peer-first-v2")] = "entry-0"

    def api_call(_kwargs):
        owner = owners.get(fingerprint_secret_value(agent.api_key))
        calls.append(owner)
        if len(calls) > 12:
            return pool_tests._mock_response("finite escape bound")
        if owner == "entry-0":
            raise pool_tests._pool_error(headers={"Retry-After": "600"})
        if owner == "entry-1" and calls.count(owner) == 1:
            _refresh_first_row(pool)
            raise pool_tests.ReadTimeout("synthetic transport interruption")
        return pool_tests._mock_response("healthy account answered")

    def rebuild(_agent, rt, **_kwargs):
        rebuilt_ids.append(owners.get(fingerprint_secret_value(rt["api_key"])))

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    monkeypatch.setattr(agent, "_try_activate_fallback", lambda **kwargs: False)
    monkeypatch.setattr("agent.agent_runtime_helpers._rebuild_primary_client", rebuild)
    monkeypatch.setattr("agent.agent_runtime_helpers.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("agent.turn_api_error.interruptible_backoff_sleep",
                        lambda _agent, seconds, *_args, **_kwargs: waits.append(seconds))
    result = agent.run_conversation("hello")
    assert calls == ["entry-0", "entry-1", "entry-1"], (calls, rebuilt_ids, waits)
    assert rebuilt_ids == ["entry-1"]
    assert result["completed"] is True and result["final_response"] == "healthy account answered"
    assert pool.entries()[0].last_status == STATUS_EXHAUSTED
    assert all(entry.last_status != STATUS_EXHAUSTED for entry in pool.entries()[1:])
    assert all(wait < 600 for wait in waits)


@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
@pytest.mark.parametrize("peer_rotated", [False, True])
def test_reinstall_snapshot_rebinds_or_clears_old_id(pooled_agent, monkeypatch, provider, peer_rotated):
    agent, pool = pooled_agent(provider, n=3)
    snapshot = agent._primary_runtime
    assert agent._recover_with_credential_pool(status_code=429, has_retried_429=False)[0] is True
    assert agent._credential_pool_entry_id == "entry-1"
    if peer_rotated:
        _refresh_first_row(pool)
    monkeypatch.setattr("agent.agent_runtime_helpers._rebuild_primary_client", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent.context_compressor, "update_model", MagicMock())
    reinstall_runtime_snapshot(agent, snapshot)
    expected_id = None if peer_rotated else "entry-0"
    assert agent._credential_pool_entry_id == expected_id
    agent._recover_with_credential_pool(status_code=429, has_retried_429=False)
    assert all(entry.last_status != STATUS_EXHAUSTED for entry in pool.entries()[1:])


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex"])
def test_duplicate_key_lap_records_the_same_row_that_pool_attributes(pooled_agent, provider):
    agent, pool = pooled_agent(provider, n=3)
    first, second, _third = pool.entries()
    pool._replace_entry(second, replace(second, access_token=first.access_token))
    second = pool.entries()[1]
    assert agent._swap_credential(second) is True
    rotated = set()
    assert agent._recover_with_credential_pool(status_code=429, has_retried_429=False,
                                             rotated_429_ids=rotated)[0] is True
    assert rotated == {"entry-1"}
    assert agent._credential_pool_entry_id == "entry-2"
    assert all(entry.last_status == STATUS_EXHAUSTED or model_cooldown_until(entry, agent.model)
               for entry in pool.entries()[:2])
