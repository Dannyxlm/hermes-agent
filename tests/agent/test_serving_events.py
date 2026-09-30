"""Typed routing events (``agent.serving_events``) are credential-free and reach the right rail."""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent import serving_events as se
from agent.credential_pool import CredentialPool, PooledCredential
from agent.error_classifier import FailoverReason


def _pool():
    entries = [
        PooledCredential.from_dict("anthropic", dict(
            id="danny1", label="DANNY-ANT", source="manual:hermes_pkce", auth_type="oauth",
            access_token="fixture-secret-danny", refresh_token="fixture-refresh-danny", priority=0)),
        PooledCredential.from_dict("anthropic", dict(
            id="sj0001", label="SJ-ANT", source="manual:hermes_pkce", auth_type="oauth",
            access_token="fixture-secret-sj", refresh_token="fixture-refresh-sj", priority=1)),
    ]
    return CredentialPool("anthropic", entries)


def _agent(events):
    pool = _pool()
    pool.set_current("danny1")
    return SimpleNamespace(
        model="claude-fable-5-1", provider="anthropic", api_mode="anthropic_messages", base_url="https://api.anthropic.com",
        api_key="fixture-secret-danny", max_tokens=8192, session_id="sess-1",
        reasoning_config={"enabled": True, "effort": "high"},
        _credential_pool=pool, _credential_pool_entry_id="danny1",
        _provider_fallback_active=False, _provider_fallback_route=None, _primary_runtime=None,
        status_callback=lambda kind, msg: events.append((kind, msg)),
    )


def _decode(events):
    assert all(kind == se.ROUTING_STATUS_KIND for kind, _ in events)
    return [json.loads(msg) for _, msg in events]


def test_account_switched_uses_labels_never_secrets():
    events = []
    agent = _agent(events)
    se.emit_account_switched(agent, from_account="DANNY-ANT", to_account="SJ-ANT",
                             reason=FailoverReason.billing, cause="rotation")
    (ev,) = _decode(events)
    assert ev["type"] == "account_switched" and ev["schema"] == 1
    assert ev["from_account"] == "DANNY-ANT" and ev["to_account"] == "SJ-ANT"
    assert ev["reason"] == "billing" and ev["reason_text"] == "credits required" and ev["cause"] == "rotation"
    assert "fixture-secret" not in json.dumps(ev) and "refresh" not in json.dumps(ev).lower()


def test_account_switched_noop_when_same_or_unknown_target():
    events = []
    agent = _agent(events)
    assert se.emit_account_switched(agent, from_account="A", to_account="A") is None
    assert se.emit_account_switched(agent, from_account="A", to_account=None) is None
    assert events == []


def test_provider_switched_and_restored_carry_serving_snapshot():
    events = []
    agent = _agent(events)
    agent.model, agent.provider, agent.api_mode = "gpt-5.5", "openai-codex", "codex_responses"
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent._provider_fallback_active, agent._provider_fallback_route = True, ("gpt-5.5", "openai-codex")
    agent._primary_runtime = {"model": "claude-fable-5-1", "provider": "anthropic"}
    se.emit_provider_switched(agent, reason=FailoverReason.rate_limit, old_model="claude-fable-5-1",
                              old_provider="anthropic", new_model="gpt-5.5", new_provider="openai-codex",
                              retry_eligible_in=1800)
    (ev,) = _decode(events)
    assert ev["type"] == "provider_switched" and ev["reason"] == "rate_limit" and ev["retry_eligible_in_s"] == 1800
    snap = ev["serving"]
    assert snap["model"] == "gpt-5.5" and snap["provider"] == "openai-codex" and snap["fallback_active"] is True
    assert snap["primary_model"] == "claude-fable-5-1" and snap["fallback_route"] == ["gpt-5.5", "openai-codex"]
    assert snap["reasoning"]["source"] == "codex_transport" and snap["reasoning"]["state"] in ("explicit", "default", "unsupported")

    events.clear()
    agent.model, agent.provider, agent.api_mode, agent.base_url = "claude-fable-5-1", "anthropic", "anthropic_messages", "https://api.anthropic.com"
    agent._provider_fallback_active, agent._provider_fallback_route = False, None
    se.emit_primary_restored(agent, previous_model="gpt-5.5", previous_provider="openai-codex")
    (ev,) = _decode(events)
    assert ev["type"] == "primary_restored" and ev["to_model"] == "claude-fable-5-1"
    assert ev["serving"]["fallback_active"] is False and ev["serving"]["account"] == "DANNY-ANT"


def test_effective_reasoning_anthropic_adaptive():
    agent = _agent([])
    r = se.effective_reasoning(agent)
    assert r["source"] == "anthropic_adapter" and r["requested"] == "high"
    assert r["state"] in ("explicit", "budget") and r["effort"]
    agent.reasoning_config = {"enabled": False}
    r = se.effective_reasoning(agent)
    assert r["enabled"] is False and r["state"] in ("disabled", "default")


def test_serving_dedupes_until_route_changes():
    events = []
    agent = _agent(events)
    assert se.emit_serving_if_changed(agent) is not None
    assert se.emit_serving_if_changed(agent) is None
    assert se.emit_serving_if_changed(agent) is None
    agent._credential_pool_entry_id = "sj0001"          # same model, other account
    assert se.emit_serving_if_changed(agent)["serving"]["account"] == "SJ-ANT"
    se.reset_serving_dedupe(agent)
    assert se.emit_serving_if_changed(agent) is not None
    assert len(events) == 3
    assert len(agent._routing_events) == 3


def test_callback_errors_never_propagate():
    def boom(kind, msg):
        raise RuntimeError("client gone")
    agent = _agent([])
    agent.status_callback = boom
    assert se.emit_serving_if_changed(agent) is not None


def test_gateway_drops_routing_kind_for_messaging_surfaces():
    from gateway.run import _prepare_gateway_status_message
    assert _prepare_gateway_status_message(None, "routing", '{"type":"serving"}') is None
    assert _prepare_gateway_status_message(None, "lifecycle", "Model fallback: x") == "Model fallback: x"
