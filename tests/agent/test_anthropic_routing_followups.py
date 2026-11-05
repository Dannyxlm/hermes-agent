"""Incident-shaped coverage of the routing follow-ups to the per-request resolver.

Model propagation shipped in 30a7ae409c; these exercise the Anthropic account
identity, not just whether an auxiliary client can be constructed.
"""
import json
import time

import pytest

from agent.credential_pool import load_pool


OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"


@pytest.fixture
def incident_pool(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for name in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    rows = [
        {"id": f"p{i}", "priority": i, "source": "manual", "auth_type": "oauth", "last_status": "ok",
         "access_token": f"fixture-account-{i}", "expires_at_ms": 4000000000000,
         "model_cooldowns": {FABLE: time.time() + 86400} if i == 0 else {}}
        for i in range(2)
    ]
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": rows}}))
    return home


@pytest.mark.parametrize("model, expected", [(OPUS, "p0"), (FABLE, None), (None, "p0")])
def test_unmatched_rotation_scopes_both_count_and_selection(incident_pool, model, expected):
    pool = load_pool("anthropic")
    entry = pool.mark_exhausted_and_rotate(
        status_code=401, api_key_hint="unknown-fixture-key", model=model,
    )
    # Fable has only one eligible row, so reporting a rotation would be false recovery.
    assert (entry.id if entry else None) == expected
    assert all(row.last_status == "ok" for row in pool.entries())


@pytest.mark.parametrize("model, expected", [(OPUS, "fixture-account-0"), (FABLE, "fixture-account-1")])
def test_auxiliary_uses_the_requested_models_account(incident_pool, monkeypatch, model, expected):
    from agent import anthropic_adapter, auxiliary_client

    # Only the external SDK boundary is replaced; pool loading, resolution and wrapping are real.
    monkeypatch.setattr(anthropic_adapter, "build_anthropic_client", lambda *_args: object())
    client, resolved = auxiliary_client.resolve_provider_client("anthropic", model=model)
    assert resolved == model
    assert client is not None
    assert client.api_key == expected


def test_auxiliary_legacy_fallback_keeps_the_requested_model(incident_pool, monkeypatch):
    from agent import anthropic_adapter, auxiliary_client

    # A pool-less auxiliary route falls back to the real resolver. The explicit
    # fixture token is known to that resolver's pool and benched only for Fable.
    monkeypatch.setenv("ANTHROPIC_TOKEN", "fixture-account-0")
    monkeypatch.setattr(auxiliary_client, "_select_pool_entry", lambda *_args, **_kwargs: (False, None))
    monkeypatch.setattr(anthropic_adapter, "build_anthropic_client", lambda *_args: object())
    client, resolved = auxiliary_client.resolve_provider_client("anthropic", model=OPUS)
    assert resolved == OPUS
    assert client is not None
    assert client.api_key == "fixture-account-0"
    assert auxiliary_client.resolve_provider_client("anthropic", model=FABLE) == (None, None)
