"""All-time RPC reads retained session counters without inventing request history."""
import sqlite3
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tui_gateway import server


def rpc(**params):
    return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "usage.rollup",
                                  "params": {"window": "all", **params}})


def result(**params):
    response = rpc(**params)
    assert "result" in response, response
    return response["result"]


def seed(db, sid, *, model="claude-opus-5-5", provider="anthropic", started=1000,
         input_tokens=100, output_tokens=20, cache_read_tokens=50, cache_write_tokens=10,
         calls=2, parent=None, source="desktop", config=None):
    db.create_session(sid, source, model=model, parent_session_id=parent, model_config=config)
    db._conn.execute("""UPDATE sessions SET started_at=?, billing_provider=?, input_tokens=?,
        output_tokens=?, cache_read_tokens=?, cache_write_tokens=?, reasoning_tokens=5,
        api_call_count=?, title=? WHERE id=?""",
        (started, provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
         calls, "Fictional " + sid, sid))
    db._conn.commit()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    with SessionDB(db_path=tmp_path / "state.db") as db:
        yield db


def test_alltime_totals_breakdowns_and_provider_coverage(store):
    seed(store, "old", started=1000)
    seed(store, "new", model="gpt-5.3-codex", provider="openai-codex", started=2000,
         input_tokens=200, output_tokens=30, cache_read_tokens=60, cache_write_tokens=0)
    store.update_token_counts("new", model="gpt-5.3-codex", billing_provider="openai-codex",
                              input_tokens=10, api_call_count=1, turn_id="ledger")
    value = result(timezone="America/Montreal")
    assert value["source"] == "session_totals"
    assert value["window"] == "all"
    assert value["start"] == value["coverage_since"] == 1000
    assert value["end"] > 2000
    assert value["timezone"] == "America/Montreal"
    assert value["partial"] is True
    assert value["coverage_reason"]
    assert value["days"] == []
    totals = value["totals"]
    assert totals["input_tokens"] == 310
    assert totals["output_tokens"] == 50
    assert totals["cache_read_tokens"] == 110
    assert totals["cache_write_tokens"] == 10
    assert totals["reasoning_tokens"] == 10
    assert totals["total_tokens"] == 480
    assert totals["api_call_count"] == 5
    assert totals["cache_hit_percent"] == pytest.approx(100 * 110 / 430)
    assert totals["estimated_cost_usd"] == pytest.approx(0.001658)
    assert totals["known_estimated_cost_usd"] == totals["estimated_cost_usd"]
    assert totals["cost_status"] == "est"
    assert totals["unknown_price_events"] == 0
    assert totals["turn_count"] is None
    assert totals["event_count"] is None
    assert totals["unattributed_turn_events"] is None
    assert value["agents"] == [{"profile": "default", **totals}]
    assert value["chat_count"] == 2
    assert {row["model"] for row in value["models"]} == {"claude-opus-5-5", "gpt-5.3-codex"}
    assert {row["provider"]: row["input_tokens"] for row in value["providers"]} == {
        "anthropic": 100, "openai-codex": 210}
    assert value["coverage"] == [
        {"provider": "anthropic", "since": 1000, "chat_count": 1, "session_count": 1},
        {"provider": "openai-codex", "since": 2000, "chat_count": 1, "session_count": 1}]
    assert [chat["session_id"] for chat in value["top_chats"]] == ["new", "old"]
    assert value["top_chats"][0]["title"] == "Fictional new"


def test_alltime_limit_filter_and_unknown_price(store):
    seed(store, "known")
    seed(store, "unknown", model="unpriced", provider="", started=500, input_tokens=500)
    value = result(limit=1)
    assert value["chat_count"] == 2
    assert len(value["top_chats"]) == 1
    assert value["top_chats"][0]["session_id"] == "unknown"
    assert value["totals"]["input_tokens"] == 600
    assert value["totals"]["estimated_cost_usd"] is None
    assert value["totals"]["unknown_price_events"] == 1
    assert value["totals"]["cost_status"] == "unknown"
    assert value["totals"]["cost_reason"]
    assert value["totals"]["known_estimated_cost_usd"] > 0
    assert value["coverage"][0]["provider"] == ""
    filtered = result(session_id="known")
    assert filtered["chat_count"] == 1
    assert filtered["totals"]["input_tokens"] == 100
    assert filtered["coverage_since"] == 1000
    assert len(filtered["coverage"]) == 1
    assert result(session_id="absent")["chat_count"] == 0


def test_alltime_owning_chat_compression_delegate_and_branch(store):
    seed(store, "root", started=1000)
    store.end_session("root", "compression")
    seed(store, "tip", started=2000, parent="root")
    seed(store, "child", started=3000, parent="tip", source="subagent")
    seed(store, "metadata-child", started=4000, parent="tip", config={"_delegate_from": "tip"})
    seed(store, "branch", started=5000, parent="root", config={"_branched_from": "root"})
    value = result(session_id="child")
    assert value["chat_count"] == 1
    assert value["totals"]["input_tokens"] == 400
    assert value["top_chats"][0]["session_id"] == "root"
    assert value["top_chats"][0]["title"] == "Fictional root"
    assert value["coverage"][0]["session_count"] == 4
    assert value["coverage"][0]["chat_count"] == 1
    assert result(session_id="branch")["totals"]["input_tokens"] == 100
    assert result()["chat_count"] == 2


def test_alltime_empty_and_zero_counter_chats(store):
    empty = result()
    assert empty["totals"]["total_tokens"] == 0
    assert empty["totals"]["estimated_cost_usd"] == 0
    assert empty["start"] is None
    assert empty["coverage_since"] is None
    assert empty["coverage"] == []
    assert empty["chat_count"] == 0
    store.create_session("zero", "desktop")
    zero = result()
    assert zero["chat_count"] == 0
    assert zero["top_chats"] == []
    assert zero["models"] == zero["providers"] == zero["coverage"] == []
    assert zero["start"] is None
    assert zero["totals"]["estimated_cost_usd"] == 0
    assert zero["totals"]["unknown_price_events"] == 0


def test_alltime_profiles_and_admission(store, tmp_path, monkeypatch):
    seed(store, "same")
    secondary = tmp_path / "secondary"
    secondary.mkdir()
    from hermes_cli import profiles
    def profile_dir(profile):
        if profile == "secondary":
            return secondary
        raise ValueError("unknown fixture profile")
    monkeypatch.setattr(profiles, "get_profile_dir", profile_dir)
    with SessionDB(db_path=secondary / "state.db") as db:
        seed(db, "same", input_tokens=800)
    assert result(profile="secondary")["totals"]["input_tokens"] == 800
    assert result()["totals"]["input_tokens"] == 100
    assert rpc(profile="missing")["error"]["code"] == 4064
    assert rpc(profile=" ")["error"]["code"] == 4002
    assert rpc(timezone="No/SuchZone")["error"]["code"] == 4002
    assert rpc(limit=True)["error"]["code"] == 4002
    assert rpc(unknown="key")["error"]["code"] == 4000


def test_alltime_no_ledger_messages_or_writes(store, monkeypatch):
    seed(store, "legacy")
    store._conn.execute("DROP TABLE usage_events")
    store._conn.execute("DROP TABLE state_meta")
    store._conn.commit()
    connect = sqlite3.connect
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX}
    def readonly_connect(*args, **kwargs):
        assert "mode=ro" in args[0]
        conn = connect(*args, **kwargs)
        def authorize(action, name, *rest):
            if action in denied or action == sqlite3.SQLITE_READ and name in {
                    "messages", "usage_events", "state_meta"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    assert result()["totals"]["input_tokens"] == 100
    assert rpc(window="today")["error"]["code"] == 5036


@pytest.mark.parametrize("window", ["today", "7d", "30d"])
def test_existing_windows_keep_event_only_shape(store, window):
    seed(store, "legacy")
    store.update_token_counts("legacy", model="claude-opus-5-5", billing_provider="anthropic",
                              input_tokens=3, api_call_count=1, turn_id="one")
    value = result(window=window)
    assert value["totals"]["input_tokens"] == 3
    assert value["totals"]["event_count"] == 1
    assert value["totals"]["turn_count"] == 1
    assert "source" not in value
    assert "coverage" not in value
    assert "window" not in value
    assert len(value["days"]) == 1


@pytest.mark.parametrize("model,provider,counts", [
    ("gpt-5.3-codex", "openai-codex", (1000000, 100000, 500000, 0)),
    ("claude-opus-5-5", "anthropic", (1000000, 100000, 500000, 50000)),
    ("gpt-6.1-sol", "openai-codex", (1000, 100, 500, 50)),
])
def test_alltime_uses_estimate_rates(store, model, provider, counts):
    keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    tokens = dict(zip(keys, counts))
    seed(store, "priced", model=model, provider=provider, **tokens)
    estimated = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "usage.estimate",
                                       "params": {"model": model, "provider": provider, **tokens}})
    assert "result" in estimated, estimated
    assert result()["totals"]["estimated_cost_usd"] == estimated["result"]["estimated_cost_usd"]


def test_alltime_never_prices_cumulative_context_as_one_request(store):
    seed(store, "aggregate", model="gpt-6.1-sol", provider="openai-codex", input_tokens=400000,
         output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, calls=2)
    totals = result()["totals"]
    assert totals["input_tokens"] == 400000
    assert totals["estimated_cost_usd"] is None
    assert totals["cost_reason"] == "per-chat totals: original call boundaries unavailable for context-tier pricing"
    assert totals["unknown_price_events"] == 1


def test_alltime_missing_store_does_not_create_it(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    before = set(Path(tmp_path).iterdir())
    assert rpc()["error"]["code"] == 5036
    assert set(Path(tmp_path).iterdir()) == before
    assert not (tmp_path / "state.db").exists()


@pytest.mark.parametrize("config,end_reason,follow", [
    ({"_branched_from": {}}, "compression", True),
    ({"_branched_from": "root"}, "compression", False),
    ({"_delegate_from": 0}, "complete", False),
    ({"_delegate_from": ""}, "complete", False),
    ({"_delegate_from": "0"}, "complete", True),
    ({"_delegate_from": []}, "complete", False),
    ({"_delegate_from": {}}, "complete", False),
    ("malformed", "complete", False),
])
def test_alltime_and_events_share_metadata_ownership(store, config, end_reason, follow):
    seed(store, "root")
    store.end_session("root", end_reason)
    seed(store, "child", parent="root", config=config if isinstance(config, dict) else None)
    if config == "malformed":
        store._conn.execute("UPDATE sessions SET model_config='{' WHERE id='child'")
        store._conn.commit()
    for sid in ("root", "child"):
        store.update_token_counts(sid, model="claude-opus-5-5", billing_provider="anthropic",
                                  input_tokens=1, api_call_count=1, turn_id=sid)
    assert result(session_id="root")["totals"]["input_tokens"] == (202 if follow else 101)
    assert result(window="today", session_id="root")["totals"]["input_tokens"] == (2 if follow else 1)


def test_alltime_single_call_keeps_estimator_context_tier(store):
    seed(store, "one-call", model="gpt-6.1-sol", provider="openai-codex", input_tokens=400000,
         output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, calls=1)
    assert result()["totals"]["estimated_cost_usd"] == pytest.approx(1.6)


def test_alltime_completed_call_without_tokens_is_unknown(store):
    seed(store, "missing-usage", input_tokens=0, output_tokens=0, cache_read_tokens=0,
         cache_write_tokens=0, calls=1)
    totals = result()["totals"]
    assert totals["estimated_cost_usd"] is None
    assert totals["unknown_price_events"] == 1
    assert "token usage unavailable" in totals["cost_reason"]


def test_alltime_auxiliary_once_without_voice_double_count(store):
    store.create_session("root", "desktop")
    store.end_session("root", "compression")
    store.create_session("child", "desktop", parent_session_id="root")
    store.update_token_counts("child", model="claude-opus-5-5", billing_provider="anthropic",
                              input_tokens=100, output_tokens=20, api_call_count=1,
                              task="voice_chat", turn_id="voice")
    store.record_auxiliary_usage("child", "background_review", model="gpt-6.1-sol",
                                 billing_provider="openai-codex", input_tokens=400000,
                                 output_tokens=1000, api_call_count=4)
    value = result(session_id="child")
    assert value["totals"]["total_tokens"] == 401120
    assert value["totals"]["api_call_count"] == 5
    assert value["chat_count"] == 1
    assert value["top_chats"][0]["session_id"] == "root"
    assert value["totals"]["cost_status"] == "minimum"
    assert value["totals"]["minimum_estimated_cost_usd"] == pytest.approx(0.8108)
    assert value["models"][0]["model"] == "gpt-6.1-sol"
    assert value["models"][0]["total_tokens"] == 401000
    assert all(row["session_count"] == 1 for row in value["coverage"])
    assert result(session_id="root")["totals"] == value["totals"]
    recent = result(window="30d", session_id="child")
    assert value["totals"]["total_tokens"] >= recent["totals"]["total_tokens"]
    assert value["totals"]["total_tokens"] == recent["totals"]["total_tokens"]


@pytest.mark.parametrize("task", ["compression", "vision", "title_generation", "web_extract", "session_search"])
def test_alltime_auxiliary_only_chat_counts(store, task):
    store.create_session("aux-only", "desktop")
    store.record_auxiliary_usage("aux-only", task, model="claude-opus-5-5",
                                 billing_provider="anthropic", input_tokens=50, output_tokens=10)
    value = result()
    assert value["totals"]["total_tokens"] == 60
    assert value["chat_count"] == 1
    assert value["coverage"][0]["session_count"] == 1
    assert value["totals"]["estimated_cost_usd"] == pytest.approx(0.0004)


@pytest.mark.parametrize("include_unknown", [False, True])
def test_alltime_minimum_on_every_envelope_and_status_precedence(store, include_unknown):
    seed(store, "mixed", input_tokens=100, output_tokens=20, cache_read_tokens=0, cache_write_tokens=0, calls=1)
    store.record_auxiliary_usage("mixed", "background_review", model="gpt-6.1-sol",
                                 billing_provider="openai-codex", input_tokens=400001,
                                 output_tokens=1001, cache_read_tokens=71, cache_write_tokens=3,
                                 api_call_count=4)
    if include_unknown:
        store.record_auxiliary_usage("mixed", "vision", model="gpt-5.5", billing_provider="openai",
                                     input_tokens=400000, cache_write_tokens=1, api_call_count=2)
    value = result()
    expected_status = "unknown" if include_unknown else "minimum"
    assert value["totals"]["cost_status"] == expected_status
    assert value["top_chats"][0]["cost_status"] == expected_status
    for row in [value["totals"], *value["agents"], *value["models"], *value["providers"], *value["top_chats"]]:
        assert "minimum_estimated_cost_usd" in row
        assert row["minimum_estimated_cost_usd"] >= 0
        if row["cost_status"] != "est":
            assert row["estimated_cost_usd"] is None
        if row.get("provider") == "openai":
            assert row["minimum_estimated_cost_usd"] == 0
    from decimal import Decimal
    minimum = Decimal(str(value["totals"]["minimum_estimated_cost_usd"]))
    exact_bound = Decimal("0.8108266")
    assert exact_bound - Decimal("0.000000000000001") <= minimum <= exact_bound
    assert value["totals"]["known_estimated_cost_usd"] == pytest.approx(0.0008)


def test_alltime_single_request_minimum_keeps_exact_tier(store):
    seed(store, "one-call", model="gpt-6.1-sol", provider="openai-codex", input_tokens=400000,
         output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, calls=1)
    totals = result()["totals"]
    assert totals["cost_status"] == "est"
    assert "minimum_estimated_cost_usd" in totals
    assert totals["minimum_estimated_cost_usd"] == pytest.approx(1.6)


def test_alltime_decimal_minimum_does_not_overshoot(store):
    from decimal import Decimal
    for index in range(31):
        seed(store, f"tier-{index}", model="gpt-6.1-sol", provider="openai-codex",
             input_tokens=400001, output_tokens=1001, cache_read_tokens=71, cache_write_tokens=3, calls=2)
    totals = result()["totals"]
    assert totals["cost_status"] == "minimum"
    expected = Decimal("0.8100266") * 31
    observed = Decimal(str(totals["minimum_estimated_cost_usd"]))
    assert expected - Decimal("0.00000000000001") <= observed <= expected
