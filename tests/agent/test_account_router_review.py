"""Review regressions: headroom, stale caps, writer races, leases and fail-open pins."""
import json
import time
from types import SimpleNamespace

import pytest

from agent.account_router.scorer import rank
from agent.account_router.snapshot import decode_meters, merge_meters, pin, read_snapshot, snapshot_path
from agent.account_router.types import AccountMeter, MeterWindow

NOW = 100000.0


def meter(id="a", windows=(), *, fetched=NOW, plan="default_claude_ai", provider="anthropic"):
    return AccountMeter(provider, id, plan, tuple(windows), fetched, "usage_api")


def test_session_headroom_prevents_fleet_hop_to_near_reset_pro():
    meters = {
        "max": meter("max", [MeterWindow("weekly", 68, NOW+47*3600),
                              MeterWindow("session", 10, NOW+3*3600)], plan="default_claude_max_20x"),
        "pro": meter("pro", [MeterWindow("weekly", 8, NOW+3*3600),
                              MeterWindow("session", 60, NOW+4*3600)]),
    }
    rows = rank("anthropic", "claude-opus-4-7", list(meters), meters, NOW, {})
    assert rows[0][0] == "max"
    scores = {id: score for id, score, _ in rows}
    assert scores["pro"] == pytest.approx((.92/3) * (.4/5))
    # Pro still leads when the Max row has little urgency left.
    meters["max"] = meter("max", [MeterWindow("weekly", 97, NOW+100*3600),
                                  MeterWindow("session", 10, NOW+3*3600)], plan="default_claude_max_20x")
    assert rank("anthropic", "claude-fable-5", list(meters), meters, NOW, {})[0][0] == "pro"


@pytest.mark.parametrize("session", [None, MeterWindow("session", None, NOW+3600)])
def test_unknown_session_does_not_penalize_weekly_urgency(session):
    windows = [MeterWindow("weekly", 8, NOW+3*3600)] + ([session] if session else [])
    result = rank("anthropic", None, ["a"], {"a": meter(windows=windows)}, NOW, {})
    assert result[0][1] == pytest.approx(.92/3)


@pytest.mark.parametrize("kind", ["session", "weekly"])
def test_expired_caps_are_not_exhausted(kind):
    windows = [MeterWindow(kind, 100, NOW-5)]
    if kind == "session":
        windows.insert(0, MeterWindow("weekly", 8, NOW+30*3600))
    score = rank("anthropic", None, ["a"], {"a": meter(windows=windows)}, NOW, {})[0][1]
    if kind == "session":
        assert score == pytest.approx(.92/30)
    else:
        assert score is None


@pytest.mark.parametrize("provider", ["anthropic", "openai-codex", "xai-oauth"])
def test_over_limit_utilization_stays_capped(provider):
    from agent.account_router import meters_anthropic, meters_codex, meters_xai
    if provider == "anthropic":
        windows = meters_anthropic.parse_usage({"seven_day": {"utilization": 101, "resets_at": NOW+3600}})
        headers = meters_anthropic.parse_headers({"anthropic-ratelimit-unified-5h-utilization": "1.2"})
        assert headers[0].used_pct == 100
    elif provider == "openai-codex":
        windows = meters_codex.parse_headers({"x-codex-secondary-used-percent": "101",
                                              "x-codex-secondary-reset-at": str(NOW+3600)}, now=NOW)
    else:
        windows = (meters_xai._window({"config": {"creditUsagePercent": 101,
                                                   "currentPeriod": {"end": NOW+3600}}}),)
    assert windows[0].used_pct == 100
    assert rank(provider, None, ["a"], {"a": meter(windows=windows, provider=provider)}, NOW, {})[0][1] == 0


@pytest.mark.parametrize("strategy,expected", [("smart", "b"), ("fill_first", "a")])
@pytest.mark.parametrize("provider", ["openai-codex", "xai-oauth"])
def test_leases_follow_smart_order_without_changing_fill_first(tmp_path, monkeypatch, strategy, expected, provider):
    from agent.credential_pool import CredentialPool, PooledCredential
    from hermes_cli.config_backend import write_config_key
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_config_key(tmp_path/"config.yaml", "credential_pool_strategies."+provider, strategy)
    entries = [PooledCredential(provider, id, id, "api_key", i, "manual", "fixture-only-"+id)
               for i, id in enumerate(["a", "b"])]
    now = time.time()
    meters = [meter(id, [MeterWindow("weekly", pct, now+86400)], fetched=now, provider=provider)
              for id, pct in [("a", 97), ("b", 1)]]
    merge_meters(snapshot_path(provider), meters, now=now, policy={})
    pool = CredentialPool(provider, entries)
    assert pool.acquire_lease() == expected
    # Lease count still takes precedence; explicit identity remains respected.
    assert pool.acquire_lease() == ("a" if expected == "b" else "b")
    assert pool.acquire_lease("a") == "a"


def test_writer_reads_uncached_to_preserve_peer_update(tmp_path, monkeypatch):
    from agent.account_router import snapshot
    path = tmp_path/"router.json"
    monkeypatch.setattr(snapshot, "_version", lambda p: (1, 1, 1))
    pin("anthropic", "a", path=path, now=NOW, policy={})
    read_snapshot(path)  # warm an intentionally colliding version
    peer = json.loads(path.read_text())
    peer["pins"]["openai-codex"] = {"entry_id": "peer", "until": NOW+3600}
    path.write_text(json.dumps(peer))
    pin("anthropic", "b", path=path, now=NOW, policy={})
    assert json.loads(path.read_text())["pins"]["openai-codex"]["entry_id"] == "peer"


def test_failed_refresh_keeps_capped_meter_until_stale(tmp_path, monkeypatch):
    from hermes_cli import auth_router
    from agent.credential_pool import PooledCredential
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    entry = PooledCredential("anthropic", "a", "Fixture", "api_key", 0, "manual", "fixture-only")
    monkeypatch.setattr(auth_router, "_entries", lambda p: [entry])
    monkeypatch.setattr(auth_router, "_fetch", lambda e: (_ for _ in ()).throw(ValueError("private-body")))
    path = snapshot_path("anthropic")
    old = meter(windows=[MeterWindow("weekly", 100, NOW+86400)])
    merge_meters(path, [old], now=NOW, policy={})
    before = path.read_bytes()
    assert auth_router.refresh(["anthropic"]) == [{"provider": "anthropic", "updated": 0, "unknown": 1}]
    assert path.read_bytes() == before
    meters = decode_meters(read_snapshot(path), "anthropic")
    assert rank("anthropic", None, ["a"], meters, NOW+1, {})[0][1] == 0
    assert rank("anthropic", None, ["a"], meters, NOW+1801, {})[0][1] is None


def test_session_units_floor_hot_reloads_from_registered_config(tmp_path, monkeypatch):
    from agent.account_router.router import load_policy
    from hermes_cli.config_backend import write_config_key
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    windows = [MeterWindow("weekly", 8, NOW+3*3600), MeterWindow("session", 60, NOW+3600)]
    meters = {"a": meter(windows=windows)}
    assert load_policy()["session_units_floor"] == 5
    initial = rank("anthropic", None, ["a"], meters, NOW, load_policy())[0][1]
    write_config_key(tmp_path/"config.yaml", "account_router.session_units_floor", 10)
    updated = rank("anthropic", None, ["a"], meters, NOW, load_policy())[0][1]
    assert updated == pytest.approx(initial/2)


def test_merge_preserves_newer_header_but_replaces_obsolete_api_windows(tmp_path):
    path = tmp_path/"router.json"
    old = meter(windows=[MeterWindow("weekly", 100, NOW+86400, "fable"),
                         MeterWindow("weekly", 20, NOW+86400)])
    merge_meters(path, [old], now=NOW, policy={})
    header = AccountMeter("anthropic", "a", None, (MeterWindow("session", 30, NOW+3600),),
                          NOW+3, "headers")
    merge_meters(path, [header], now=NOW+3, partial=True, policy={})
    # The full API source no longer publishes Fable. Only the later session reading survives.
    full = meter(windows=[MeterWindow("weekly", 60, NOW+86400)], fetched=NOW+2)
    merge_meters(path, [full], now=NOW+5, policy={})
    got = decode_meters(read_snapshot(path), "anthropic")["a"]
    assert {(w.kind, w.model_scope, w.used_pct) for w in got.windows} == {
        ("session", None, 30), ("weekly", None, 60)}
    assert got.fetched_at == NOW+3


def test_older_full_fetch_does_not_resurrect_a_removed_scoped_window(tmp_path):
    path = tmp_path/"router.json"
    newer = meter(windows=[MeterWindow("weekly", 20, NOW+86400)], fetched=NOW+10)
    merge_meters(path, [newer], now=NOW+10, policy={})
    older = meter(windows=[MeterWindow("weekly", 100, NOW+86400, "fable")], fetched=NOW)
    merge_meters(path, [older], now=NOW+11, policy={})
    got = decode_meters(read_snapshot(path), "anthropic")["a"]
    assert [(w.model_scope, w.used_pct) for w in got.windows] == [(None, 20)]


@pytest.mark.parametrize("strategy,corrupt", [("fill_first", None), ("smart", "{not json"), ("smart", '{"version":2}')])
def test_priority_saved_even_when_pin_is_disabled_or_fails(tmp_path, monkeypatch, strategy, corrupt, caplog):
    from agent.credential_pool import CredentialPool, PooledCredential
    from hermes_cli import auth_commands
    from hermes_cli.config_backend import write_config_key
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_config_key(tmp_path/"config.yaml", "credential_pool_strategies.anthropic", strategy)
    pool = CredentialPool("anthropic", [PooledCredential("anthropic", id, id, "api_key", i, "manual", "fixture-only-"+id)
                                        for i, id in enumerate(["a", "b"])])
    monkeypatch.setattr(auth_commands, "load_pool", lambda p: pool)
    path = snapshot_path("anthropic")
    if corrupt:
        path.parent.mkdir(parents=True)
        path.write_text(corrupt)
    caplog.set_level("DEBUG")
    auth_commands.auth_priority_command(SimpleNamespace(provider="anthropic", target="b", priority=0))
    assert json.loads((tmp_path/"auth.json").read_text())["credential_pool"]["anthropic"][0]["id"] == "b"
    if corrupt:
        assert path.read_text() == corrupt
        assert "pin" in caplog.text.lower()
    else:
        assert not path.exists()


def test_header_capture_failure_does_not_skip_rate_limit_parser(monkeypatch):
    from agent.account_router import meters_anthropic
    from agent.rate_limit_credits import RateLimitCreditsMixin
    monkeypatch.setattr(meters_anthropic, "capture_headers", lambda *a: (_ for _ in ()).throw(ValueError("fixture")))
    agent = RateLimitCreditsMixin()
    agent.provider = "anthropic"
    agent._capture_rate_limits(SimpleNamespace(headers={"x-ratelimit-limit-tokens": "100", "x-ratelimit-remaining-tokens": "42"}))
    assert agent.get_rate_limit_state().tokens_min.remaining == 42
