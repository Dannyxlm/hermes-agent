"""``_try_refresh_anthropic_client_credentials`` must keep failure attribution on the account
that is actually on the wire.

The pre-request resolver can hand the agent ANOTHER pooled account's token (operator priority
edit, cooldown on the previous account). Before this fix the agent swapped ``_anthropic_api_key``
but left ``api_key`` and ``_credential_pool_entry_id`` on the old account, so the next 4xx was
blamed on -- and benched -- the wrong credential.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.credential_pool import CredentialPool, PooledCredential
from run_agent import AIAgent

DANNY = "fixture-token-danny-max"
SJ = "fixture-token-sj-pro"


def _pool():
    entries = [
        PooledCredential.from_dict("anthropic", dict(
            id="danny1", label="DANNY-ANT", source="manual:hermes_pkce", auth_type="oauth",
            access_token=DANNY, refresh_token="r-danny", priority=0)),
        PooledCredential.from_dict("anthropic", dict(
            id="sj0001", label="SJ-ANT", source="manual:hermes_pkce", auth_type="oauth",
            access_token=SJ, refresh_token="r-sj", priority=1)),
    ]
    return CredentialPool("anthropic", entries)


@pytest.fixture
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
    a.api_mode = "anthropic_messages"
    a.provider = "anthropic"
    a._anthropic_base_url = "https://api.anthropic.com"
    a._anthropic_client = MagicMock()
    a._is_anthropic_oauth = True
    pool = _pool()
    pool.set_current("danny1")
    a._credential_pool = pool
    a._credential_pool_entry_id = "danny1"
    a.api_key = a._anthropic_api_key = DANNY
    return a


def test_refresh_to_other_account_rebinds_attribution(agent):
    with (
        patch("agent.anthropic_credentials.resolve_anthropic_token", return_value=SJ),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        assert agent._try_refresh_anthropic_client_credentials() is True

    assert agent._anthropic_api_key == SJ
    assert agent.api_key == SJ, "attribution key must follow the wire token"
    assert agent._credential_pool_entry_id == "sj0001"
    assert agent._credential_pool.current().id == "sj0001"


def test_failed_entry_identity_names_the_wire_account(agent):
    """End-to-end: after the silent switch, the pool attributes a 429 to SJ, not DANNY."""
    with (
        patch("agent.anthropic_credentials.resolve_anthropic_token", return_value=SJ),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        agent._try_refresh_anthropic_client_credentials()
    from agent.agent_runtime_helpers import _failed_credential_identity
    api_key_hint, credential_id = _failed_credential_identity(agent, agent._credential_pool)
    assert api_key_hint == SJ and credential_id == "sj0001"
    entry = agent._credential_pool._identify_failed_entry(credential_id, api_key_hint)
    assert entry is not None and entry.label == "SJ-ANT"


def test_same_token_is_noop(agent):
    with patch("agent.anthropic_credentials.resolve_anthropic_token", return_value=DANNY):
        assert agent._try_refresh_anthropic_client_credentials() is False
    assert agent._credential_pool_entry_id == "danny1"
    assert agent.api_key == DANNY


def test_token_outside_pool_clears_entry_id(agent):
    """A token the pool does not know (env override) must not keep the stale id."""
    with (
        patch("agent.anthropic_credentials.resolve_anthropic_token", return_value="fixture-env-token"),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        assert agent._try_refresh_anthropic_client_credentials() is True
    assert agent.api_key == "fixture-env-token"
    assert agent._credential_pool_entry_id is None
    # Cursor untouched: nothing in the pool matched, so we must not move it arbitrarily.
    assert agent._credential_pool.current().id == "danny1"


def test_no_pool_bound_is_safe(agent):
    agent._credential_pool = None
    agent._credential_pool_entry_id = None
    with (
        patch("agent.anthropic_credentials.resolve_anthropic_token", return_value=SJ),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        assert agent._try_refresh_anthropic_client_credentials() is True
    assert agent.api_key == SJ and agent._credential_pool_entry_id is None
