"""The per-request Anthropic resolver honours only the requested model's cooldowns.

Production incident (2026-10-29): the priority-0 Claude account got a Fable-only
``credits_required`` 429 (its plan has no usage credits), so the pool benched it for
``claude-fable-5-1`` only. ``resolve_anthropic_token(model=<opus>)`` enumerated the pool
without the model, ``model_cooldown_until(entry, None)`` treated the Fable bench as
blocking, and every turn's pre-request refresh swapped Opus traffic to the priority-1
account for days. Real ``agent.credential_pool`` against a real temp auth store.
"""
import json
import time

import pytest

PRIMARY = "fixture-oauth-primary"
SECONDARY = "fixture-oauth-secondary"
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    (tmp_path / "fakehome").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "fakehome"))
    for var in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]
    far = int((time.time() + 3600) * 1000)
    bench_until = time.time() + 2 * 86400

    def write(primary_cooldowns):
        rows = [
            {"id": "primary1", "label": "PRIMARY-ANT", "auth_type": "oauth", "priority": 0,
             "source": "manual:hermes_pkce", "access_token": PRIMARY, "refresh_token": "r-primary",
             "expires_at_ms": far, "model_cooldowns": primary_cooldowns},
            {"id": "secondary1", "label": "SECONDARY-ANT", "auth_type": "oauth", "priority": 1,
             "source": "manual:hermes_pkce", "access_token": SECONDARY, "refresh_token": "r-secondary",
             "expires_at_ms": far},
        ]
        (root / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": rows}}))

    write({FABLE: bench_until})
    return write


def test_other_model_bench_does_not_divert_requested_model(store):
    from agent.anthropic_credentials import resolve_anthropic_token

    assert resolve_anthropic_token(model=OPUS) == PRIMARY, "a Fable-only bench must not move Opus traffic"
    assert resolve_anthropic_token(model=FABLE) == SECONDARY, "the benched model still skips the row"


def test_model_less_resolution_stays_conservative(store):
    from agent.anthropic_credentials import resolve_anthropic_token

    # Callers that name no model keep honouring every active cooldown (pool default).
    assert resolve_anthropic_token() == SECONDARY


def test_bench_on_requested_model_still_rotates(store):
    from agent.anthropic_credentials import resolve_anthropic_token

    store({OPUS: time.time() + 3600})
    assert resolve_anthropic_token(model=OPUS) == SECONDARY
    assert resolve_anthropic_token(model=FABLE) == PRIMARY


def test_expired_bench_is_ignored(store):
    from agent.anthropic_credentials import resolve_anthropic_token

    store({FABLE: time.time() - 60})
    assert resolve_anthropic_token(model=OPUS) == PRIMARY
    assert resolve_anthropic_token() == PRIMARY
