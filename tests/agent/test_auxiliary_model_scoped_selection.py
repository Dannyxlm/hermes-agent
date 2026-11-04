"""Model entitlement benches must not disable unrelated auxiliary work.

Real temp auth stores and routing; no provider requests or live credentials.
"""
import json
import time
from types import SimpleNamespace

import pytest

from agent import auxiliary_client as aux
from agent.credential_pool import load_pool


@pytest.fixture
def auth_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    home = tmp_path / "hermes"
    home.mkdir()
    rows = [
        {"id": f"seat-{i}", "priority": i, "source": "manual", "auth_type": "oauth",
         "expires_at_ms": 4000000000000,
         "access_token": f"fixture-account-{i}", "model_cooldowns": {
             "gpt-6.1": time.time() + 31536000, "claude-fable-5-1": time.time() + 31536000,
         }}
        for i in range(3)
    ]
    (home / "auth.json").write_text(json.dumps({"credential_pool": {
        "openai-codex": rows, "anthropic": rows,
    }}))
    (home / "config.yaml").write_text(json.dumps({"model": {
        "provider": "anthropic", "default": "claude-haiku-4-5-20251001",
    }}))
    aux.clear_runtime_main()
    aux.shutdown_cached_clients()
    return home


@pytest.mark.parametrize("task", ["vision", "compression"])
@pytest.mark.parametrize("provider, model", [
    ("openai-codex", "gpt-6-luna"), ("anthropic", "claude-haiku-4-5-20251001"),
])
def test_auxiliary_resolves_only_the_requested_model(auth_store, task, provider, model):
    with aux.aux_probe_mode():
        if task == "vision":
            _, client, resolved = aux.resolve_vision_provider_client(provider, model)
        else:
            client, resolved = aux.resolve_provider_client(provider, model, task=task)
        assert client is not None
        assert resolved == model
        blocked_model = "gpt-6.1" if provider == "openai-codex" else "claude-fable-5-1"
        assert load_pool(provider).select(model=blocked_model) is None
        blocked, _ = aux.resolve_provider_client(provider, blocked_model, task=task)
        assert blocked is None


def test_model_less_selection_ignores_only_model_benches(auth_store, monkeypatch):
    pool = load_pool("openai-codex")
    assert pool.select() is not None
    monkeypatch.setattr(type(pool), "_codex_quota_restored_upstream", lambda self, entry: False)
    for entry in pool.entries():
        pool.mark_exhausted_and_rotate(status_code=402, credential_id=entry.id, failure_reason="billing")
    assert pool.select() is None
    assert pool.select(model="gpt-6-luna") is None
    with aux.aux_probe_mode():
        assert aux.resolve_provider_client("openai-codex", "gpt-6-luna") == (None, None)


@pytest.mark.parametrize("provider, model", [
    ("openai-codex", "gpt-6-luna"), ("anthropic", "claude-haiku-4-5-20251001"),
])
def test_credential_wide_exhaustion_blocks_requests_and_explicit_leases(auth_store, monkeypatch, provider, model):
    pool = load_pool(provider)
    monkeypatch.setattr(type(pool), "_codex_quota_restored_upstream", lambda self, entry: False)
    for entry in pool.entries():
        pool.mark_exhausted_and_rotate(status_code=402, credential_id=entry.id, failure_reason="billing")
    assert pool.select(model=model) is None
    assert pool.acquire_lease("seat-0") is None
    assert pool.acquire_lease("seat-0", model=model) is None
    with aux.aux_probe_mode():
        assert aux.resolve_provider_client(provider, model) == (None, None)


def test_unmatched_rotation_and_child_lease_obey_requested_model(auth_store):
    pool = load_pool("openai-codex")
    # Only one entry may serve this model. Unmatched recovery must not call that a rotation.
    for entry in pool.entries()[:2]:
        pool.mark_exhausted_and_rotate(status_code=400, credential_id=entry.id,
                                      failure_reason="model_entitlement", model="gpt-6-luna")
    assert pool.mark_exhausted_and_rotate(status_code=401, api_key_hint="unmatched-fixture",
                                          model="gpt-6-luna") is None
    from tools.delegate_tool_child_run import _lease_child_credential
    bound = []
    child = SimpleNamespace(_credential_pool=pool, model="gpt-6-luna", provider="openai-codex",
                            base_url="https://chatgpt.com/backend-api/codex", _swap_credential=bound.append)
    _, leased = _lease_child_credential(child)
    assert leased == "seat-2"
    assert bound[0].id == leased
    pool.release_lease(leased)


@pytest.mark.parametrize("async_mode", [False, True])
def test_vision_rescue_does_not_send_codex_model_to_anthropic(auth_store, async_mode):
    # Persist the failed task model too: a rescue must not re-import it from config.
    (auth_store / "config.yaml").write_text(json.dumps({
        "model": {"provider": "anthropic", "default": "claude-haiku-4-5-20251001"},
        "auxiliary": {"vision": {"provider": "openai-codex", "model": "gpt-6-luna"}},
    }))
    # All Codex rows refuse the configured vision model; auto rescue uses the main Anthropic route.
    pool = load_pool("openai-codex")
    for entry in pool.entries():
        pool.mark_exhausted_and_rotate(status_code=400, credential_id=entry.id,
                                      failure_reason="model_entitlement", model="gpt-6-luna")
    with aux.aux_probe_mode():
        route = aux._resolve_call_client(
            "vision", provider="openai-codex", model="gpt-6-luna", base_url=None, api_key=None,
            resolved_provider="openai-codex", resolved_model="gpt-6-luna", resolved_base_url=None,
            resolved_api_key=None, resolved_api_mode=None, main_runtime={
                "provider": "anthropic", "model": "claude-haiku-4-5-20251001",
            }, async_mode=async_mode,
        )
    assert route.effective_provider == "anthropic"
    assert route.final_model and route.final_model.startswith("claude-")


def test_text_auto_fallback_keeps_the_destination_model(auth_store):
    (auth_store / "config.yaml").write_text(json.dumps({
        "model": {"provider": "openai-codex", "default": "gpt-6.1"},
        "fallback_providers": [{"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}],
    }))
    with aux.aux_probe_mode():
        client, model = aux.resolve_provider_client("auto", "gpt-6.1", task="compression", main_runtime={
            "provider": "openai-codex", "model": "gpt-6.1",
        })
    assert client is not None
    assert model == "claude-haiku-4-5-20251001"

