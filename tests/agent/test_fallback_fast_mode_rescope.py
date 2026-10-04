"""Fallback activation re-scopes pinned fast-mode overrides to the new route.

Regression: static ``/fast`` on Anthropic pins ``speed: "fast"`` into
``request_overrides``. A fast-mode 429 then activated a Codex fallback that rejected
``speed`` ("unsupported field(s): speed") and an xAI fallback whose SDK raised
"unexpected keyword argument 'speed'", so the whole fallback chain died.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.fast_mode import rescope_fast_mode_after_fallback
from run_agent import AIAgent


def _state(model, provider, overrides, service_tier: "str | None" = "priority", api_mode="chat_completions"):
    return SimpleNamespace(
        model=model,
        provider=provider,
        base_url="https://example.invalid/v1",
        api_mode=api_mode,
        service_tier=service_tier,
        request_overrides=dict(overrides),
    )


def _resolver(result):
    return patch("hermes_cli.models.resolve_fast_mode_overrides", return_value=result)


def test_anthropic_speed_is_dropped_for_a_route_without_fast_support():
    agent = _state("gpt-6-sol", "openai-codex", {"speed": "fast", "extra_body": {"k": 1}})
    with _resolver(None):
        rescope_fast_mode_after_fallback(agent)
    assert agent.request_overrides == {"extra_body": {"k": 1}}


def test_static_fast_repins_the_new_routes_own_override():
    agent = _state("grok-4.7", "xai-oauth", {"speed": "fast"})
    with _resolver({"service_tier": "priority"}):
        rescope_fast_mode_after_fallback(agent)
    assert agent.request_overrides == {"service_tier": "priority"}


def test_anthropic_to_anthropic_fallback_keeps_speed():
    agent = _state("claude-opus-5-5", "anthropic", {"speed": "fast"}, api_mode="anthropic_messages")
    with _resolver({"speed": "fast"}):
        rescope_fast_mode_after_fallback(agent)
    assert agent.request_overrides == {"speed": "fast"}


def test_non_fast_caller_values_survive():
    agent = _state("gpt-6-sol", "openai-codex", {"service_tier": "flex"}, service_tier=None)
    with _resolver({"service_tier": "priority"}):
        rescope_fast_mode_after_fallback(agent)
    assert agent.request_overrides == {"service_tier": "flex"}


def test_normal_mode_turn_is_untouched_and_not_newly_pinned():
    agent = _state("grok-4.7", "xai-oauth", {"extra_body": {"a": 1}}, service_tier=None)
    with _resolver({"service_tier": "priority"}):
        rescope_fast_mode_after_fallback(agent)
    assert agent.request_overrides == {"extra_body": {"a": 1}}


def test_resolver_failure_still_strips_fast_keys():
    agent = _state("grok-4.7", "xai-oauth", {"speed": "fast"})
    with patch("hermes_cli.models.resolve_fast_mode_overrides", side_effect=RuntimeError("boom")):
        rescope_fast_mode_after_fallback(agent)
    assert agent.request_overrides == {}


def _make_agent(fallback_model):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def test_try_activate_fallback_strips_speed_end_to_end():
    agent = _make_agent({"provider": "openrouter", "model": "openai/gpt-6-sol"})
    agent.service_tier = "priority"
    agent.request_overrides = {"speed": "fast"}
    fb_client = MagicMock()
    fb_client.base_url = "https://openrouter.ai/api/v1"
    fb_client.api_key = "fb-key"
    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fb_client, "openai/gpt-6-sol")),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        patch("hermes_cli.models.resolve_fast_mode_overrides", return_value=None),
    ):
        assert agent._try_activate_fallback() is True
    assert "speed" not in (agent.request_overrides or {})
