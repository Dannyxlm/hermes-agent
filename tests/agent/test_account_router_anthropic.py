"""Anthropic usage/profile and serving-account unified header contracts."""
import threading
import time
from types import SimpleNamespace

from agent.account_router import meters_anthropic as ant
from agent.account_router.snapshot import decode_meters, merge_meters, read_snapshot, snapshot_path
from agent.account_router.types import AccountMeter, MeterWindow


def test_scoped_limits_and_legacy_payloads():
    windows = ant.parse_usage({"limits": [
        {"kind": "session", "percent": 7, "resets_at": 110000},
        {"kind": "weekly_all", "percent": 30, "resets_at": 120000},
        {"kind": "weekly_scoped", "percent": 100, "resets_at": 130000,
         "scope": {"model": {"display_name": "Claude Fable"}}}]})
    assert [(w.kind, w.model_scope, w.used_pct) for w in windows] == [
        ("session", None, 7), ("weekly", None, 30), ("weekly", "fable", 100)]
    assert ant.parse_usage({"seven_day_sonnet": {"utilization": 90, "resets_at": 120000}})[0].model_scope == "sonnet"


def test_fetch_uses_saved_bearer_and_persisted_plan_cache(monkeypatch):
    from agent import account_usage
    calls = []
    def get(url, headers, *, timeout):
        assert headers["Authorization"] == "Bearer fixture-only"
        assert timeout == 5
        calls.append(url)
        if url.endswith("profile"):
            return {"organization": {"rate_limit_tier": "default_claude_max_5x"}}
        return {"limits": [{"kind": "weekly_scoped", "percent": 12, "resets_at": time.time()+86400,
                            "scope": {"model": {"display_name": "Fable"}}}]}
    monkeypatch.setattr(account_usage, "_get_json", get)
    entry = SimpleNamespace(id="a", access_token="fixture-only")
    first = ant.fetch(entry)
    assert first.plan == "default_claude_max_5x"
    merge_meters(snapshot_path("anthropic"), [first], policy={})
    second = ant.fetch(entry)
    assert second.windows[0].model_scope == "fable"
    assert sum(url.endswith("profile") for url in calls) == 1
    assert sum(url.endswith("usage") for url in calls) == 2


def test_live_headers_async_throttled_and_serving_entry(monkeypatch):
    from agent.rate_limit_credits import RateLimitCreditsMixin
    from agent import memory_provider
    captured = threading.Event()
    real_spawn = memory_provider.spawn_context_thread
    started = []
    def spawn(fn, **kwargs):
        def wrapped():
            try:
                fn()
            finally:
                captured.set()
        thread = real_spawn(wrapped, **kwargs)
        started.append(thread)
        return thread
    monkeypatch.setattr(memory_provider, "spawn_context_thread", spawn)
    agent = RateLimitCreditsMixin()
    agent.provider = "anthropic"
    agent._credential_pool = SimpleNamespace(_strategy="smart")
    agent._credential_pool_entry_id = "serving"
    headers = {"Anthropic-Ratelimit-Unified-5h-Utilization": "0.42",
               "anthropic-ratelimit-unified-5h-reset": str(time.time()+18000),
               "anthropic-ratelimit-unified-7d-fable-utilization": "1",
               "anthropic-ratelimit-unified-7d-fable-reset": str(time.time()+86400)}
    response = SimpleNamespace(headers=headers)
    agent._capture_rate_limits(response)
    assert captured.wait(5)
    agent._capture_rate_limits(response)
    assert len(started) == 1
    windows = decode_meters(read_snapshot(snapshot_path("anthropic")), "anthropic")["serving"].windows
    assert [(w.used_pct, w.model_scope) for w in windows] == [(42, None), (100, "fable")]
    assert all(w.resets_at > time.time() for w in windows)
    started[0].join(5)


def test_smart_off_headers_have_no_sidecar(monkeypatch):
    agent = SimpleNamespace(provider="anthropic", _credential_pool=SimpleNamespace(_strategy="fill_first"),
                            _credential_pool_entry_id="a")
    ant.capture_headers(agent, {"anthropic-ratelimit-unified-5h-utilization": ".9"})
    assert not snapshot_path("anthropic").exists()
