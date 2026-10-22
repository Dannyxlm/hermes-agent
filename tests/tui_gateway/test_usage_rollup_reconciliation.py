"""Public rollup regressions and shipped UsageRollup.swift decode constraints."""
import datetime
import math

import pytest

from hermes_state import SessionDB
from tui_gateway import server

_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
             "reasoning_tokens", "api_call_count")
_NOW = datetime.datetime(2026, 10, 22, 16, tzinfo=datetime.UTC)
_START = _NOW.replace(hour=0).timestamp()


def _read(row, name, kind, *, nullable=False, optional=False):
    if optional and name not in row:
        return None
    value = row[name]
    if nullable and value is None:
        return None
    assert isinstance(value, kind) and (not isinstance(value, bool) or kind is bool), name
    if kind is int:
        assert -(2**63) <= value < 2**63, name
    return value


def _metrics(row):
    for key in (*_COUNTERS, "total_tokens", "unknown_price_events"):
        assert _read(row, key, int) >= 0, key
    for key in ("turn_count", "event_count", "unattributed_turn_events"):
        value = _read(row, key, int, nullable=True)
        assert value is None or value >= 0, key
    percent = _read(row, "cache_hit_percent", (int, float), nullable=True)
    assert percent is None or math.isfinite(percent) and 0 <= percent <= 100
    exact = _read(row, "estimated_cost_usd", (int, float), nullable=True)
    for key in ("estimated_cost_usd", "known_estimated_cost_usd", "minimum_estimated_cost_usd"):
        value = _read(row, key, (int, float), nullable=key != "known_estimated_cost_usd",
                      optional=key == "minimum_estimated_cost_usd")
        assert value is None or math.isfinite(value) and value >= 0, key
    status = _read(row, "cost_status", str)
    reason = _read(row, "cost_reason", str, nullable=True)
    assert status in {"est", "minimum", "unknown"}
    assert exact is not None if status == "est" else exact is None and bool(reason)
    for key in ("pricing_dates", "price_sources"):
        assert all(isinstance(value, str) for value in _read(row, key, list))


def _shipped_decode(value):
    """Port every rollup DTO read and require rule from the shipped Swift model."""
    profile = _read(value, "profile", str)
    start = _read(value, "start", (int, float), nullable=True)
    end = _read(value, "end", (int, float))
    zone = _read(value, "timezone", str)
    assert _read(value, "basis", str) == "standard_api_list_price"
    assert _read(value, "currency", str) == "USD"
    since = _read(value, "coverage_since", (int, float), nullable=True)
    _read(value, "partial", bool)
    reason = _read(value, "coverage_reason", str)
    window = _read(value, "window", str, nullable=True, optional=True)
    source = _read(value, "source", str, nullable=True, optional=True)
    coverage = _read(value, "coverage", list, nullable=window != "all", optional=window != "all") or []
    days = _read(value, "days", list, nullable=window == "all", optional=window == "all") or []
    assert window != "all" or source == "session_totals" and not days
    _metrics(_read(value, "totals", dict))
    for row in _read(value, "agents", list):
        assert _read(row, "profile", str)
        _metrics(row)
    for row in _read(value, "models", list):
        _read(row, "model", str)
        _read(row, "provider", str)
        _metrics(row)
    for row in _read(value, "providers", list):
        _read(row, "provider", str)
        _metrics(row)
    for row in days:
        _metrics(row)
        assert _read(row, "day", str) and _read(row, "provider", str)
    for row in _read(value, "top_chats", list):
        assert _read(row, "session_id", str)
        _read(row, "title", str, nullable=True)
        _metrics(row)
    for row in coverage:
        _read(row, "provider", str)
        assert math.isfinite(_read(row, "since", (int, float)))
        assert _read(row, "chat_count", int) >= 0
        assert _read(row, "session_count", int) >= 0
    assert profile and (start is not None or window == "all")
    assert start is None or math.isfinite(start) and start <= end
    assert math.isfinite(end) and zone and reason
    assert since is None or math.isfinite(since)
    assert _read(value, "chat_count", int) >= 0


@pytest.fixture
def store(tmp_path, monkeypatch):
    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW.astimezone(tz) if tz else _NOW.replace(tzinfo=None)

    monkeypatch.setattr(datetime, "datetime", Clock)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db._conn.execute("UPDATE state_meta SET value=? WHERE key='usage_events_since'",
                         (str(_START + 3600),))
        db._conn.commit()
        yield db


def _legacy(db, sid, *, tokens=0, provider="anthropic"):
    db.create_session(sid, "desktop", model="claude-opus-5-5")
    db._conn.execute("UPDATE sessions SET started_at=?, billing_provider=?, input_tokens=?, "
                     "api_call_count=? WHERE id=?", (_START + 100, provider, tokens, int(tokens > 0), sid))
    db._conn.commit()


def _aux(db, sid, *, provider="anthropic", base_url="https://x", mode="", task="vision", counts=None):
    counts = counts or (100, 0, 0, 0, 0, 1)
    db._conn.execute("INSERT INTO session_model_usage (session_id, model, billing_provider, "
                     "billing_base_url, billing_mode, task, input_tokens, output_tokens, cache_read_tokens, "
                     "cache_write_tokens, reasoning_tokens, api_call_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, "claude-opus-5-5", provider, base_url, mode, task, *counts))
    db._conn.commit()


def _rollup(window):
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "usage.rollup",
                                      "params": {"window": window, "timezone": "UTC"}})
    assert "result" in response, response
    return response["result"]


@pytest.mark.parametrize("window", ["today", "7d", "30d", "all"])
@pytest.mark.parametrize("source", ["events", "backfill", "mixed"])
def test_legacy_missing_providers_decode_every_row(store, window, source):
    legacy_tokens = 0 if source == "events" else 13
    _legacy(store, "legacy", tokens=legacy_tokens, provider=None)
    if source == "events":
        store._conn.execute("UPDATE state_meta SET value=? WHERE key='usage_events_since'", (str(_START - 1),))
        store._conn.commit()
    if source in {"backfill", "mixed"}:
        _aux(store, "legacy", provider="")
    if source in {"events", "mixed"}:
        for task in ("vision", "title_generation"):
            store.record_auxiliary_usage("legacy", task, model="claude-opus-5-5", input_tokens=7,
                usage_events=[{"input_tokens": 7, "api_call_count": 1, "usage_timestamp": _START + 7200}])
    value = _rollup(window)
    _shipped_decode(value)
    for key in ("models", "providers", "days", "coverage"):
        for row in value.get(key, []):
            assert row["provider"] == "unknown"
    assert value["totals"]["input_tokens"] == legacy_tokens + (100 if source in {"backfill", "mixed"} else 0) + (
        14 if source in {"events", "mixed"} else 0)


@pytest.mark.parametrize("task", ["voice_chat", "future_route"])
def test_voice_route_double_count(store, task):
    _legacy(store, "voice", tokens=1000, provider="openai-codex")
    store.update_token_counts("voice", model="gpt-6.1-sol", billing_provider="openai-codex",
        input_tokens=500, api_call_count=1, usage_timestamp=_START + 7200, turn_id="t1", task=task)
    value = _rollup("7d")
    assert value["totals"]["input_tokens"] == 1500
    assert value["totals"]["api_call_count"] == 2
    assert value["totals"]["event_count"] == 1
    for key in ("models", "providers", "days", "top_chats"):
        assert sum(row["input_tokens"] for row in value[key]) == 1500


@pytest.mark.parametrize("split", ["base_url", "billing_mode"])
def test_multi_row_aux(store, split):
    _legacy(store, "aux")
    _aux(store, "aux", base_url="https://x", mode="legacy" if split == "billing_mode" else "")
    store.record_auxiliary_usage("aux", "vision", model="claude-opus-5-5", billing_provider="anthropic",
        billing_base_url="https://x" if split == "billing_mode" else "https://y", input_tokens=100,
        usage_events=[{"input_tokens": 100, "api_call_count": 1, "usage_timestamp": _START + 7200}])
    value = _rollup("7d")
    assert value["totals"]["input_tokens"] == 200
    assert value["totals"]["api_call_count"] == 2
    assert value["totals"]["event_count"] == 1
    for key in ("models", "providers", "days", "top_chats"):
        assert sum(row["input_tokens"] for row in value[key]) == 200
