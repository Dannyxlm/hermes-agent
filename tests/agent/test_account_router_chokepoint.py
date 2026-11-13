"""Black-box regressions that run unchanged against the pre-router credential pool."""
import json
import time

from agent.credential_pool import CredentialPool, PooledCredential, get_pool_strategy


def test_smart_strategy_config_is_recognized(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("credential_pool_strategies:\n  anthropic: smart\n")
    assert get_pool_strategy("anthropic") == "smart"


def test_anthropic_direct_available_entries_uses_shared_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("credential_pool_strategies:\n  anthropic: smart\n")
    rows = [{"id": id, "label": id, "priority": i, "auth_type": "api_key", "source": "manual",
             "access_token": "fixture-only-"+id} for i, id in enumerate(["a", "b"])]
    (tmp_path / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": rows}}))
    now = time.time()
    meters = {id: {"provider": "anthropic", "entry_id": id, "plan": "default_claude_ai",
                   "windows": [{"kind": "weekly", "used_pct": used, "resets_at": now+86400,
                                "model_scope": "fable"}], "fetched_at": now, "source": "usage_api", "extras": {}}
              for id, used in [("a", 99), ("b", 8)]}
    path = tmp_path / "state/account_router.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1, "meters": {"anthropic": meters},
                               "decisions": {"anthropic": {"fable": {"order": ["b", "a"], "decided_at": now}}}}))
    before = path.read_bytes()
    pool = CredentialPool("anthropic", [PooledCredential.from_dict("anthropic", r) for r in rows])
    available, _pending = pool._available_entries(model="claude-fable-5-2")
    assert [e.id for e in available] == ["b", "a"]
    assert path.read_bytes() == before
