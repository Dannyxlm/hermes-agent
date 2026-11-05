"""Anthropic delegated children must lease the row eligible for their model."""
import json
import time
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("model, expected", [("claude-opus-5-5", "p0"), ("claude-fable-5-1", "p1"), (None, "p0")])
def test_child_lease_and_bound_account_agree(tmp_path, monkeypatch, model, expected):
    from agent.credential_pool import load_pool
    from tools.delegate_tool_child_run import _lease_child_credential

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for name in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    rows = [
        {"id": f"p{i}", "priority": i, "source": "manual", "auth_type": "oauth",
         "access_token": f"fixture-account-{i}", "expires_at_ms": 4000000000000,
         "model_cooldowns": {"claude-fable-5-1": time.time() + 86400} if i == 0 else {}}
        for i in range(2)
    ]
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": rows}}))
    pool = load_pool("anthropic")
    bound = []
    child = SimpleNamespace(
        _credential_pool=pool, provider="anthropic", model=model,
        base_url="https://api.anthropic.com", _swap_credential=bound.append,
    )
    leased_pool, lease_id = _lease_child_credential(child)
    try:
        assert leased_pool is pool
        assert lease_id == expected
        assert [entry.id for entry in bound] == [expected]
        assert pool._active_leases == {expected: 1}
    finally:
        if lease_id is not None:
            pool.release_lease(lease_id)
    assert pool._active_leases == {}
