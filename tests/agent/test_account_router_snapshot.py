"""Shared hysteresis, pin expiry, monotonic header merges and profile isolation."""
import json
from dataclasses import replace

from agent.account_router.router import decide, ordered_ids
from agent.account_router.snapshot import decode_meters, merge_meters, pin, read_snapshot, snapshot_path
from agent.account_router.types import AccountMeter, MeterWindow

NOW = 100000.0


def m(id, pct, now=NOW, scope=None):
    return AccountMeter("anthropic", id, "default_claude_ai",
                        (MeterWindow("weekly", pct, now+86400, scope),), now, "usage_api")


def test_dwell_margin_and_capped_current():
    old = {"order": ["a", "b"], "decided_at": NOW}
    meters = {"a": m("a", 50), "b": m("b", 0)}
    assert decide("anthropic", None, ["a", "b"], meters, old, NOW+1, {})["order"][0] == "a"
    assert decide("anthropic", None, ["a", "b"], meters, old, NOW+7201,
                  {"max_meter_age_minutes": 180})["order"][0] == "b"
    meters["a"] = m("a", 99)
    assert decide("anthropic", None, ["a", "b"], meters, old, NOW+1, {})["order"][0] == "b"
    assert decide("anthropic", None, ["b"], meters, old, NOW+1, {})["order"] == ["b"]
    meters["a"] = m("a", 5)
    assert decide("anthropic", None, ["a", "b"], meters, old, NOW+7201,
                  {"max_meter_age_minutes": 180})["order"][0] == "a"


def test_pin_expiry_and_model_unavailable(tmp_path):
    path = tmp_path / "router.json"
    meters = {"a": m("a", 90), "b": m("b", 0)}
    merge_meters(path, list(meters.values()), now=NOW, policy={})
    pin("anthropic", "a", path=path, now=NOW, policy={"pin_hours": 1})
    state = read_snapshot(path)
    assert ordered_ids("anthropic", None, ["a", "b"], meters, state, NOW+1, {})[0] == "a"
    assert ordered_ids("anthropic", "claude-fable-5", ["b"], meters, state, NOW+1, {}) == ["b"]
    assert ordered_ids("anthropic", None, ["a", "b"], meters, state, NOW+3601,
                       {"max_meter_age_minutes": 90})[0] == "b"


def test_header_delta_does_not_launder_old_scoped_window(tmp_path):
    from agent.account_router.scorer import rank
    path = tmp_path / "router.json"
    api = replace(m("a", 100), windows=(MeterWindow("weekly", 100, NOW+86400, "fable"),
                                        MeterWindow("weekly", 0, NOW+86400)))
    merge_meters(path, [api], now=NOW, policy={})
    header = replace(m("a", 10, NOW+2000), source="headers")
    merge_meters(path, [header], now=NOW+2000, partial=True, policy={})
    merged = decode_meters(read_snapshot(path), "anthropic")
    assert len(merged["a"].windows) == 2
    assert merged["a"].plan == api.plan
    assert rank("anthropic", "claude-fable-5", ["a"], merged, NOW+2000, {})[0][1] is None
    assert rank("anthropic", "claude-opus-4", ["a"], merged, NOW+2000, {})[0][1] > 0
    merge_meters(path, [m("a", 80)], now=NOW+2001, policy={})
    assert decode_meters(read_snapshot(path), "anthropic")["a"].fetched_at == NOW+2000


def test_profile_ownership_and_mtime_cache(tmp_path, monkeypatch):
    from hermes_cli import auth
    home = tmp_path / "profile"
    root = tmp_path / "root"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: root / "auth.json")
    assert snapshot_path("anthropic") == root / "state/account_router.json"
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [{"id": "local"}]}}))
    assert snapshot_path("anthropic") == home / "state/account_router.json"
    assert snapshot_path("openai-codex") == root / "state/account_router.json"
    path = snapshot_path("anthropic")
    merge_meters(path, [m("a", 40)], now=NOW, policy={})
    cached = read_snapshot(path)
    assert read_snapshot(path) is cached
    pin("anthropic", "a", path=path, now=NOW, policy={})
    assert read_snapshot(path) is not cached


def test_fresh_session_cap_is_not_hidden_by_stale_weekly():
    from agent.account_router.scorer import rank
    meter = AccountMeter("anthropic", "a", "default_claude_ai",
                         (MeterWindow("weekly", 20, NOW+86400), MeterWindow("session", 100, NOW+18000)),
                         NOW+2000, "headers", {"window_fetched_at": {"weekly/all": NOW}})
    assert rank("anthropic", None, ["a"], {"a": meter}, NOW+2000, {})[0][1] == 0


def test_header_throttle_is_enforced_in_shared_store(tmp_path):
    path = tmp_path / "router.json"
    merge_meters(path, [m("a", 10)], now=NOW, policy={}, partial=True)
    before = path.stat().st_mtime_ns
    merge_meters(path, [m("a", 20, NOW+59)], now=NOW+59, policy={}, partial=True)
    assert path.stat().st_mtime_ns == before
    assert decode_meters(read_snapshot(path), "anthropic")["a"].windows[0].used_pct == 10
    merge_meters(path, [m("a", 30, NOW+60)], now=NOW+60, policy={}, partial=True)
    assert decode_meters(read_snapshot(path), "anthropic")["a"].windows[0].used_pct == 30


def test_policy_hot_reload_ignores_frozen_model_config(tmp_path, monkeypatch):
    from agent.account_router.router import load_policy
    from hermes_cli.config_backend import write_config_key
    from hermes_cli.runtime_provider import _FROZEN_RUNTIME_CONFIG
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path.write_text("account_router:\n  cap_pct: 90\n")
    token = _FROZEN_RUNTIME_CONFIG.set({"account_router": {"cap_pct": 5}})
    try:
        assert load_policy()["cap_pct"] == 90
        write_config_key(path, "account_router.cap_pct", 97)
        assert load_policy()["cap_pct"] == 97
    finally:
        _FROZEN_RUNTIME_CONFIG.reset(token)


def test_cross_process_decisions_from_same_auth(tmp_path):
    import os
    import subprocess
    import sys
    import time
    home = tmp_path / "home"
    home.mkdir()
    entries = [{"id": id, "label": id, "auth_type": "api_key", "access_token": "fixture-"+id,
                "source": "manual", "priority": i} for i, id in enumerate(["a", "b"])]
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": entries}}))
    (home / "config.yaml").write_text("credential_pool_strategies:\n  anthropic: smart\n")
    now = time.time()
    path = home / "state/account_router.json"
    merge_meters(path, [m("a", 90, now), m("b", 0, now)], now=now, policy={})
    script = '''from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli.auth import read_credential_pool
p = CredentialPool("anthropic", [PooledCredential.from_dict("anthropic", r) for r in read_credential_pool("anthropic")])
print(p._available_entries(model="claude-fable-5")[0][0].id)
'''
    env = {"HOME": str(home), "HERMES_HOME": str(home), "PATH": os.environ["PATH"]}
    results = [subprocess.check_output([sys.executable, "-c", script], env=env, text=True).strip() for _ in range(2)]
    assert results == ["b", "b"]
