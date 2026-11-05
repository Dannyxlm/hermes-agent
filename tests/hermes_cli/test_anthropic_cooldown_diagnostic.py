"""Pool-only model cooldowns are not missing Anthropic credentials."""
import json
import time

import pytest


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1"])
def test_pool_only_model_cooldown_has_rate_limit_diagnostic(tmp_path, monkeypatch, model):
    from hermes_cli.runtime_provider import AuthError, _anthropic_token_or_raise

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for name in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    path = home / "auth.json"
    path.write_text(json.dumps({"credential_pool": {"anthropic": [{
        "id": "p0", "priority": 0, "source": "manual", "auth_type": "oauth",
        "access_token": "fixture-account-0", "expires_at_ms": 4000000000000,
        "model_cooldowns": {model: time.time() + 86400},
    }]}}))
    before = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises(AuthError, match=f"rate-limited for {model}"):
        _anthropic_token_or_raise(model=model)
    other = "claude-fable-5-1" if model == "claude-opus-5-5" else "claude-opus-5-5"
    assert _anthropic_token_or_raise(model=other) == "fixture-account-0"
    assert _anthropic_token_or_raise() == "fixture-account-0"
    # This normalized, healthy store needs no maintenance; not a universal read-only guarantee.
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
