"""Windowed usage reads metadata only and never recharges absolute checkpoints."""
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server


def rpc(**params):
    return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "usage.rollup", "params": params})


def test_rollup_is_available_before_any_usage(tmp_path, monkeypatch):
    with SessionDB(db_path=tmp_path / "state.db"):
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        value = rpc(window="7d")
        assert "result" in value, value
        assert value["result"]["totals"]["total_tokens"] == 0


def test_events_have_window_and_model_attribution(tmp_path, monkeypatch):
    now = time.time()
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        db.create_session("one", "desktop", model="gpt-6.1-sol")
        for model, stamp, turn in [("gpt-6.1-sol", now - 60, "t1"), ("gpt-6.1-sol", now - 30, "t1"),
                                  ("gpt-5.3-codex", now - 86400 * 10, "old")]:
            db.update_token_counts("one", model=model, billing_provider="openai-codex", input_tokens=1000,
                                   output_tokens=100, api_call_count=1, usage_timestamp=stamp, turn_id=turn)
        db.update_token_counts("one", input_tokens=3000, output_tokens=300, absolute=True)
        value = rpc(window="7d")["result"]
        assert value["totals"]["input_tokens"] == 2000
        assert value["totals"]["turn_count"] == 1
        assert value["totals"]["api_call_count"] == 2
        assert value["models"][0]["model"] == "gpt-6.1-sol"
        assert value["top_chats"][0]["session_id"] == "one"
        assert value["totals"]["estimated_cost_usd"] == pytest.approx(0.006)
        assert rpc(window="30d")["result"]["totals"]["input_tokens"] == 3000


def test_read_path_refuses_transcript_reads_and_writes(tmp_path, monkeypatch):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("one", "desktop")
        db.update_token_counts("one", input_tokens=50, model="unknown", api_call_count=1)
    from hermes_state_rollup import read_usage_rollup
    conn = sqlite3.connect(f"file:{tmp_path / 'state.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    def authorizer(action, arg1, arg2, *rest):
        if action == sqlite3.SQLITE_READ and arg1 == "messages":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorizer)
    value = read_usage_rollup(conn, start=time.time() - 86400, end=time.time(), profile="default")
    assert value["totals"]["total_tokens"] == 50
    assert value["totals"]["estimated_cost_usd"] is None
    assert value["totals"]["unknown_price_events"] == 1
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM usage_events WHERE occurred_at>=? AND occurred_at<?",
                        (0, time.time())).fetchall()
    assert any("INDEX" in row[3] for row in plan)
    conn.close()


def test_coalesced_accounting_keeps_each_call_and_timestamp(tmp_path, monkeypatch):
    now = time.time()
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        db.create_session("chat", "desktop")
        common = {"model": "gpt-6.1-sol", "billing_provider": "openai-codex", "api_call_count": 1}
        batch = [("chat", {**common, "input_tokens": 200000, "usage_timestamp": now - 5, "turn_id": "t1"}),
                 ("chat", {**common, "input_tokens": 200000, "usage_timestamp": now - 1, "turn_id": "t2"})]
        db._apply_token_batch(batch)
        value = rpc()["result"]["totals"]
        assert value["event_count"] == 2
        assert value["turn_count"] == 2
        # Each call is SHORT context. Pricing a coalesced 400K prompt would double it.
        assert value["estimated_cost_usd"] == pytest.approx(0.8)
        stamps = [r[0] for r in db._conn.execute("SELECT occurred_at FROM usage_events ORDER BY occurred_at")]
        assert stamps == [now - 5, now - 1]


def test_profiles_are_isolated_and_bad_params_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    secondary = tmp_path / "secondary"
    secondary.mkdir()
    monkeypatch.setattr(server, "_profile_home", lambda profile: secondary if profile == "secondary" else None)
    for home, tokens in [(tmp_path, 100), (secondary, 800)]:
        with SessionDB(db_path=home / "state.db") as db:
            db.create_session("same-id", "desktop")
            db.update_token_counts("same-id", input_tokens=tokens, api_call_count=1, turn_id="t")
    assert rpc(profile="secondary")["result"]["totals"]["input_tokens"] == 800
    assert rpc()["result"]["totals"]["input_tokens"] == 100
    for params in [{"window": "90d"}, {"limit": -1}, {"timezone": "No/SuchZone"}, {"window": 7}]:
        assert rpc(**params)["error"]["code"] == 4002


def test_chat_filter_limit_auxiliary_and_unknown_are_honest(tmp_path, monkeypatch):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        for sid, tokens in [("a", 10), ("b", 100)]:
            db.create_session(sid, "desktop")
            db.update_token_counts(sid, model="gpt-6.1-sol", billing_provider="openai-codex",
                                   input_tokens=tokens, api_call_count=1, turn_id="turn-" + sid)
        db.record_auxiliary_usage("a", "vision", model="unpriced", input_tokens=20)
        result = rpc(limit=1)["result"]
        assert result["totals"]["input_tokens"] == 130
        assert result["chat_count"] == 2
        assert len(result["top_chats"]) == 1
        assert result["top_chats"][0]["session_id"] == "b"
        assert result["totals"]["estimated_cost_usd"] is None
        assert result["totals"]["cost_reason"]
        assert result["totals"]["turn_count"] == 2
        assert rpc(session_id="a")["result"]["totals"]["input_tokens"] == 30
        assert result["partial"] is True


def test_calendar_day_uses_requested_timezone(tmp_path):
    from datetime import UTC, datetime
    from hermes_state_rollup import read_usage_rollup
    stamp = datetime(2026, 10, 19, 2, tzinfo=UTC).timestamp()
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("chat", "desktop")
        db.update_token_counts("chat", input_tokens=1, usage_timestamp=stamp)
        result = read_usage_rollup(db._conn, start=stamp - 1, end=stamp + 1,
                                   profile="default", timezone_name="America/Montreal")
        assert result["days"][0]["day"] == "2026-10-18"


def test_absent_store_is_not_created_by_request(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    assert rpc()["error"]["code"] == 5036
    assert not (tmp_path / "state.db").exists()


def test_compression_delegation_and_explicit_branch_attribution(tmp_path, monkeypatch):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        db.create_session("root", "desktop")
        db.end_session("root", "compression")
        db.create_session("tip", "desktop", parent_session_id="root")
        db.create_session("child", "desktop", parent_session_id="tip",
                          model_config={"_delegate_from": "tip"})
        db.create_session("fork", "desktop", parent_session_id="root",
                          model_config={"_branched_from": "root"})
        for sid, tokens in [("tip", 10), ("child", 20), ("fork", 40)]:
            db.update_token_counts(sid, model="gpt-6.1-sol", billing_provider="openai-codex",
                                   input_tokens=tokens, api_call_count=1, turn_id="turn-" + sid)
        chats = {row["session_id"]: row["input_tokens"] for row in rpc()["result"]["top_chats"]}
        assert chats == {"root": 30, "fork": 40}
        assert rpc(session_id="tip")["result"]["totals"]["input_tokens"] == 30


@pytest.mark.parametrize("with_events", [False, True])
def test_background_review_aggregate_requires_original_call_boundaries(tmp_path, monkeypatch, with_events):
    from agent.background_review import _record_review_usage_to_parent
    now = time.time()
    usage = {"model": "gpt-6.1-sol", "provider": "openai-codex", "input_tokens": 400000, "api_calls": 2}
    stamps = [now - 10, now - 5]
    if with_events:
        usage["usage_events"] = [{"input_tokens": 200000, "api_call_count": 1, "usage_timestamp": stamp}
                                 for stamp in stamps]
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        db.create_session("parent", "desktop")
        parent = SimpleNamespace(_session_db=db, session_id="parent")
        _record_review_usage_to_parent(parent, usage)
        result = rpc()["result"]["totals"]
        assert result["input_tokens"] == 400000
        assert result["api_call_count"] == 2
        if with_events:
            assert result["estimated_cost_usd"] == pytest.approx(0.8)
            assert result["event_count"] == 2
            recorded = [row[0] for row in db._conn.execute("SELECT occurred_at FROM usage_events ORDER BY occurred_at")]
            assert recorded == stamps
        else:
            assert result["estimated_cost_usd"] is None
            assert result["unknown_price_events"] == 1
            assert "call boundaries" in result["cost_reason"]
            assert result["event_count"] == 1
        # The auxiliary checkpoint must still leave the parent's main-loop counters alone.
        assert db.get_session("parent")["input_tokens"] == 0


@pytest.mark.parametrize("model,provider,input_tokens,output_tokens,expected", [
    ("claude-opus-5-5", "anthropic", 100000, 10000, 0.6),
    ("claude-opus-5-5", "anthropic", 1000000, 100000, 6.0),
    ("gpt-6.1-sol", "openai-codex", 100000, 10000, 0.3),
    ("gpt-6.1-sol", "openai-codex", 272000, 10000, 0.644),
    ("gpt-6.1-sol", "openai-codex", 272001, 10000, None),
])
def test_auxiliary_aggregate_writer_only_nulls_rate_sensitive_usage(
        tmp_path, monkeypatch, model, provider, input_tokens, output_tokens, expected):
    from agent.background_review import _record_review_usage_to_parent
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        db.create_session("parent", "desktop")
        parent = SimpleNamespace(_session_db=db, session_id="parent")
        _record_review_usage_to_parent(parent, {"model": model, "provider": provider,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "api_calls": 2})
        stored = db._conn.execute("SELECT estimated_cost_usd, price_reason FROM usage_events").fetchone()
        totals = rpc()["result"]["totals"]
        if expected is None:
            assert stored[0] is None
            assert "call boundaries" in stored[1]
            assert totals["estimated_cost_usd"] is None
            assert totals["cost_status"] == "minimum"
            assert totals["minimum_estimated_cost_usd"] == pytest.approx(0.644002)
        else:
            assert stored[0] == pytest.approx(expected)
            assert stored[1] is None
            assert totals["estimated_cost_usd"] == pytest.approx(expected)
            assert totals["cost_status"] == "est"
        assert totals["api_call_count"] == 2
        assert totals["event_count"] == 1
        assert db.get_session("parent")["input_tokens"] == 0


@pytest.mark.parametrize("profile", ["default", "secondary"])
@pytest.mark.parametrize("window", ["today", "7d", "30d"])
@pytest.mark.parametrize("include_unknown", [False, True])
def test_historical_nulls_reprice_before_every_group_without_writes(
        tmp_path, monkeypatch, profile, window, include_unknown):
    from decimal import Decimal
    from hermes_cli import profiles
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    secondary = tmp_path / "secondary"
    secondary.mkdir()
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: secondary if name == "secondary" else tmp_path)
    home = tmp_path if profile == "default" else secondary
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("mixed", "desktop")
        db.record_auxiliary_usage("mixed", "background_review", model="claude-opus-5-5",
                                  billing_provider="anthropic", input_tokens=100000,
                                  output_tokens=10000, api_call_count=2)
        db.record_auxiliary_usage("mixed", "background_review", model="gpt-6.1-sol",
                                  billing_provider="openai-codex", input_tokens=400001,
                                  output_tokens=1001, cache_read_tokens=71, cache_write_tokens=3,
                                  api_call_count=4)
        if include_unknown:
            db.record_auxiliary_usage("mixed", "vision", model="unpriced", input_tokens=100)
            db.record_auxiliary_usage("mixed", "title_generation", model="claude-opus-5-5",
                                      billing_provider="anthropic", api_call_count=1)
        db._conn.execute("UPDATE usage_events SET estimated_cost_usd=NULL, "
                         "price_reason='aggregate usage: original call boundaries unavailable'")
        db.update_token_counts("mixed", model="claude-opus-5-5", billing_provider="anthropic",
                               input_tokens=100, api_call_count=1, turn_id="exact")
        # Stored exact prices remain authoritative when the table changes.
        db._conn.execute("UPDATE usage_events SET estimated_cost_usd=0.123456789 WHERE turn_id='exact'")
        db._conn.commit()
        before = list(db._conn.execute("SELECT * FROM usage_events ORDER BY id"))
        connect = sqlite3.connect
        denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX}
        def readonly_connect(*args, **kwargs):
            assert "mode=ro" in args[0]
            conn = connect(*args, **kwargs)
            def authorize(action, name, *rest):
                if action in denied or action == sqlite3.SQLITE_READ and name == "messages":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        monkeypatch.setattr(sqlite3, "connect", readonly_connect)
        response = rpc(profile=profile, window=window, session_id="mixed")
        assert "result" in response, response
        value = response["result"]
        totals = value["totals"]
        expected_status = "unknown" if include_unknown else "minimum"
        assert totals["known_estimated_cost_usd"] == pytest.approx(0.723456789)
        assert totals["cost_status"] == expected_status
        assert totals["unknown_price_events"] == (3 if include_unknown else 1)
        assert totals["event_count"] == (5 if include_unknown else 3)
        expected_bound = Decimal("1.533483389")
        observed = Decimal(str(totals["minimum_estimated_cost_usd"]))
        assert expected_bound - Decimal("0.000000000000001") <= observed <= expected_bound
        for row in [totals, *value["agents"], *value["models"], *value["providers"],
                    *value["days"], *value["top_chats"]]:
            assert row["minimum_estimated_cost_usd"] >= 0
            assert bool(row["cost_reason"]) == (row["cost_status"] != "est")
            if row["cost_status"] != "est":
                assert row["estimated_cost_usd"] is None
        for key in ("agents", "models", "providers", "days", "top_chats"):
            assert sum(row["minimum_estimated_cost_usd"] for row in value[key]) == pytest.approx(float(expected_bound))
            assert sum(row["known_estimated_cost_usd"] for row in value[key]) == pytest.approx(0.723456789)
        assert value["agents"][0]["profile"] == profile
        assert list(db._conn.execute("SELECT * FROM usage_events ORDER BY id")) == before


def test_timestamped_decimal_minimum_never_overshoots(tmp_path, monkeypatch):
    from decimal import Decimal
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("many", "desktop")
        for _ in range(31):
            db.record_auxiliary_usage("many", "background_review", model="gpt-6.1-sol",
                                      billing_provider="openai-codex", input_tokens=400001,
                                      output_tokens=1001, cache_read_tokens=71, cache_write_tokens=3,
                                      api_call_count=2)
        totals = rpc()["result"]["totals"]
        expected = Decimal("0.8100266") * 31
        observed = Decimal(str(totals["minimum_estimated_cost_usd"]))
        assert expected - Decimal("0.00000000000001") <= observed <= expected


def test_large_window_prices_each_null_event_once_per_request(tmp_path, monkeypatch):
    from unittest.mock import Mock
    import hermes_state_rollup as rollup
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("many", "desktop")
        db.update_token_counts("many", model="claude-opus-5-5", billing_provider="anthropic",
                               input_tokens=100, api_call_count=1, turn_id="stored")
        db._conn.execute("""WITH RECURSIVE n(i) AS (
            SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<8200
        ) INSERT INTO usage_events (
            session_id, chat_id, occurred_at, model, provider, task,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            reasoning_tokens, api_call_count, estimated_cost_usd, pricing_date)
        SELECT 'many', 'many', ?, 'claude-opus-5-5', 'anthropic', 'background_review',
            i, 0, 0, 0, 0, 2, NULL, '2026-10-19' FROM n""", (time.time() - 1,))
        db._conn.commit()
        price = Mock(wraps=rollup.estimate_aggregate_api_value_decimal)
        monkeypatch.setattr(rollup, "estimate_aggregate_api_value_decimal", price)
        for _ in range(2):
            price.reset_mock()
            value = rpc(window="30d")["result"]
            assert value["totals"]["event_count"] == 8201
            assert value["totals"]["cost_status"] == "est"
            assert value["totals"]["estimated_cost_usd"] == pytest.approx(134.4968)
            assert price.call_count == 8200
            for key in ("agents", "models", "providers", "days", "top_chats"):
                assert sum(row["event_count"] for row in value[key]) == 8201
