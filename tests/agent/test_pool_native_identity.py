"""Native Anthropic request attribution must survive a peer's persisted OAuth rotation."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.credential_persistence import fingerprint_secret_value
from agent.credential_pool import EXHAUSTED_TTL_429_SECONDS, load_pool
from agent.credential_pool_model_cooldowns import model_cooldown_until
from hermes_cli.auth import read_credential_pool, write_credential_pool
from run_agent import AIAgent
from tests.agent import test_fallback_429_after_timeout as pool_tests

MODEL = "claude-opus-5-5"


def _response():
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="healthy account answered")],
                           stop_reason="end_turn", usage=None, model=MODEL)


@pytest.fixture
def native_agent(monkeypatch):
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)
    monkeypatch.setattr("agent.anthropic_adapter.build_anthropic_client", lambda *args, **kwargs: MagicMock())
    rows = [dict(id=f"entry-{i}", label=f"account-{i}", priority=i,
                 source="manual:hermes_pkce", auth_type="oauth",
                 access_token=f"fixture-bearer-{i}-v1", refresh_token=f"fixture-grant-{i}-v1",
                 expires_at_ms=int((time.time() + 86400) * 1000)) for i in range(3)]
    write_credential_pool("anthropic", rows)
    pool = load_pool("anthropic")
    first = pool.select(model=MODEL)
    with (
        patch("model_tools.get_tool_definitions", return_value=pool_tests._make_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(api_key=first.runtime_api_key, base_url="https://api.anthropic.com",
                        provider="anthropic", model=MODEL, credential_pool=pool,
                        api_mode="anthropic_messages", quiet_mode=True, skip_context_files=True,
                        skip_memory=True, fallback_model=[])
    for name in ("_persist_session", "_save_trajectory", "_cleanup_task_resources"):
        monkeypatch.setattr(agent, name, MagicMock())
    agent._disable_streaming = True
    agent._api_max_retries = 2
    monkeypatch.setattr(agent, "_try_activate_fallback", lambda **kwargs: False)
    monkeypatch.setattr(agent, "_try_recover_primary_transport", lambda *args, **kwargs: False)
    return agent, pool


@pytest.mark.parametrize("cursor", [None, "entry-1"])
@pytest.mark.parametrize("hint,seconds", [("reset", 7200), ("short", 60), ("missing", EXHAUSTED_TTL_429_SECONDS)])
def test_native_peer_rotation_binds_wire_identity_before_429(native_agent, monkeypatch, cursor, hint, seconds):
    agent, pool = native_agent
    # Keep the long-running pool stale; only the peer's disk row is refreshed.
    rows = read_credential_pool("anthropic")
    rows[0].update(access_token="fixture-bearer-0-v2", refresh_token="fixture-grant-0-v2")
    write_credential_pool("anthropic", rows)
    pool.set_current(cursor)
    owners = {fingerprint_secret_value(f"fixture-bearer-{i}-v{v}"): f"entry-{i}"
              for i in range(3) for v in (1, 2)}
    calls, ids, waits = [], [], []
    now = time.time()
    headers = {"reset": {"Retry-After": "7200"}, "short": {"Retry-After": "0.01"}, "missing": {}}[hint]

    def api_call(_kwargs):
        # The real resolver runs at the native request boundary, not a patched identity helper.
        agent._create_request_anthropic_client(reason="native_identity_regression")
        owner = owners.get(fingerprint_secret_value(agent.api_key))
        calls.append(owner)
        ids.append(agent._credential_pool_entry_id)
        if owner == "entry-0" and len(calls) <= 12:
            raise pool_tests._pool_error(headers=headers)
        return _response()

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    monkeypatch.setattr("agent.turn_api_error.interruptible_backoff_sleep",
                        lambda _agent, seconds, *_args, **_kwargs: waits.append(seconds))
    result = agent.run_conversation("hello")
    assert calls == ["entry-0", "entry-1"], (calls, ids, waits)
    assert ids == calls
    assert waits == [] and result["completed"] is True
    fresh = {entry.id: entry for entry in load_pool("anthropic").entries()}
    assert model_cooldown_until(fresh["entry-0"], MODEL) >= now + seconds
    assert model_cooldown_until(fresh["entry-0"], "sibling-model") is None
    assert all(model_cooldown_until(fresh[i], MODEL) is None for i in ("entry-1", "entry-2"))
    assert fingerprint_secret_value(fresh["entry-0"].access_token) == fingerprint_secret_value("fixture-bearer-0-v2")
    assert fingerprint_secret_value(fresh["entry-0"].refresh_token) == fingerprint_secret_value("fixture-grant-0-v2")


def test_native_external_token_switch_does_not_blame_pool(native_agent, monkeypatch):
    agent, pool = native_agent
    monkeypatch.setenv("ANTHROPIC_TOKEN", "fixture-external-bearer")
    agent._create_request_anthropic_client(reason="external_identity_control")
    assert agent._credential_pool_entry_id is None
    assert fingerprint_secret_value(agent.api_key) == fingerprint_secret_value("fixture-external-bearer")
    agent._recover_with_credential_pool(status_code=429, has_retried_429=False)
    assert all(not entry.model_cooldowns for entry in pool.entries())
