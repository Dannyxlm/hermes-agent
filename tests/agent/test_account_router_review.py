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
    assert scores["pro"] == pytest.approx((.92/3) * (.4/2.5))
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
    assert load_policy()["session_units_floor"] == 2.5
    initial = rank("anthropic", None, ["a"], meters, NOW, load_policy())[0][1]
    write_config_key(tmp_path/"config.yaml", "account_router.session_units_floor", 5)
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


def test_fable_scope_scores_zero_for_plan_omitting_fable_window_when_pool_has_fable():
    meters = {
        "max": meter("max", [MeterWindow("weekly", 35, NOW + 136 * 3600),
                             MeterWindow("weekly", 28, NOW + 136 * 3600, "fable")],
                     plan="default_claude_max_5x"),
        "pro": meter("pro", [MeterWindow("weekly", 10, NOW + 1 * 3600)],
                     plan="default_claude_ai"),
    }
    fable_rows = rank("anthropic", "claude-fable-5-2", ["pro", "max"], meters, NOW, {})
    assert fable_rows[0][0] == "max"
    assert fable_rows[1] == ("pro", 0.0, "model scope unavailable on plan")
    # Non-Fable traffic still scores the Pro account normally.
    opus_scores = {id: score for id, score, _ in rank("anthropic", "claude-opus-5-5", ["pro", "max"], meters, NOW, {})}
    assert opus_scores["pro"] > 0


def test_fable_scope_preserves_header_only_snapshot_and_unscoped_pool():
    # Header-only partial snapshot without usage_fetched_at is not falsely marked unentitled.
    meters = {
        "max": meter("max", [MeterWindow("weekly", 35, NOW + 48 * 3600),
                             MeterWindow("weekly", 28, NOW + 48 * 3600, "fable")],
                     plan="default_claude_max_5x"),
        "headers_only": AccountMeter("anthropic", "headers_only", "default_claude_max_5x",
                                     (MeterWindow("weekly", 10, NOW + 24 * 3600),), NOW, "headers"),
    }
    scores = {id: score for id, score, _ in rank("anthropic", "claude-fable-5-2", ["max", "headers_only"], meters, NOW, {})}
    assert scores["headers_only"] > 0
    # When no account in the pool publishes a fable window, no entry is zeroed out.
    unscoped = {"a": meter("a", [MeterWindow("weekly", 10, NOW + 24 * 3600)], plan="default_claude_ai")}
    assert rank("anthropic", "claude-fable-5-2", ["a"], unscoped, NOW, {})[0][1] > 0


def test_max_5x_normal_session_use_keeps_full_score_and_tapers_above_half():
    base = 0.65 * 5 / 100
    low_session = meter("a", [MeterWindow("weekly", 35, NOW + 100 * 3600),
                              MeterWindow("session", 35, NOW + 3 * 3600)], plan="default_claude_max_5x")
    half_session = meter("b", [MeterWindow("weekly", 35, NOW + 100 * 3600),
                               MeterWindow("session", 50, NOW + 3 * 3600)], plan="default_claude_max_5x")
    tight_session = meter("c", [MeterWindow("weekly", 35, NOW + 100 * 3600),
                                MeterWindow("session", 70, NOW + 3 * 3600)], plan="default_claude_max_5x")
    scores = {id: s for id, s, _ in rank("anthropic", "claude-opus-5-5", ["a", "b", "c"],
                                         {"a": low_session, "b": half_session, "c": tight_session}, NOW, {})}
    assert scores["a"] == pytest.approx(base)
    assert scores["b"] == pytest.approx(base)
    assert scores["c"] == pytest.approx(base * 0.6)


def test_fable_capped_account_boosted_for_non_fable_scopes_until_weekly_cap():
    meters = {
        "max5": meter("max5", [MeterWindow("weekly", 35, NOW + 136 * 3600),
                               MeterWindow("weekly", 28, NOW + 136 * 3600, "fable")],
                      plan="default_claude_max_5x"),
        "max20_fable_capped": meter("max20_fable_capped",
                                    [MeterWindow("weekly", 96, NOW + 48 * 3600),
                                     MeterWindow("weekly", 100, NOW + 48 * 3600, "fable")],
                                    plan="default_claude_max_20x"),
    }
    # Opus/Sonnet/all prefer the Fable-capped account to drain its non-Fable weekly headroom first.
    for model in (None, "claude-opus-5-5", "claude-sonnet-5-5"):
        rows = rank("anthropic", model, ["max5", "max20_fable_capped"], meters, NOW, {})
        assert rows[0][0] == "max20_fable_capped"
        assert "fable capped" in rows[0][2]
    # Fable still skips the Fable-capped account.
    fable_rows = rank("anthropic", "claude-fable-5-2", ["max5", "max20_fable_capped"], meters, NOW, {})
    assert fable_rows[0][0] == "max5"
    assert fable_rows[1][1] == 0.0
    # Disabling the boost via policy restores pure base urgency order.
    disabled = rank("anthropic", "claude-opus-5-5", ["max5", "max20_fable_capped"], meters, NOW,
                    {"fable_capped_non_fable_boost": 1.0})
    assert disabled[0][0] == "max5"
    # Once weekly_all itself hits cap_pct, score drops to 0.0 despite the boost.
    meters["max20_fable_capped"] = meter("max20_fable_capped",
                                         [MeterWindow("weekly", 98, NOW + 48 * 3600),
                                          MeterWindow("weekly", 100, NOW + 48 * 3600, "fable")],
                                         plan="default_claude_max_20x")
    capped_rows = rank("anthropic", "claude-opus-5-5", ["max5", "max20_fable_capped"], meters, NOW, {})
    assert capped_rows[0][0] == "max5"
    assert capped_rows[1][1] == 0.0
    # An expired Fable cap does not apply the boost.
    meters["max20_fable_capped"] = meter("max20_fable_capped",
                                         [MeterWindow("weekly", 96, NOW + 48 * 3600),
                                          MeterWindow("weekly", 100, NOW - 10, "fable")],
                                         plan="default_claude_max_20x")
    expired = rank("anthropic", "claude-opus-5-5", ["max5", "max20_fable_capped"], meters, NOW, {})
    assert expired[0][0] == "max5"
