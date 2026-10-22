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
        assert result["partial"] is False
        assert result["backfill"]["session_count"] == 0


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


def legacy_session(db, sid, started, *, tokens=100, model="claude-opus-5-5",
                   provider="anthropic", parent=None, source="desktop", config=None):
    db.create_session(sid, source, model=model, parent_session_id=parent, model_config=config)
    db._conn.execute("UPDATE sessions SET started_at=?, billing_provider=?, input_tokens=?, "
                     "output_tokens=10, cache_read_tokens=20, cache_write_tokens=5, "
                     "reasoning_tokens=3, api_call_count=2, title=? WHERE id=?",
                     (started, provider, tokens, "Fictional " + sid, sid))
    db._conn.commit()


def set_coverage(db, since):
    db._conn.execute("UPDATE state_meta SET value=? WHERE key='usage_events_since'", (str(since),))
    db._conn.commit()


def readonly_rollup(path, *, start, end, **params):
    from hermes_state_rollup import read_usage_rollup
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        def authorize(action, name, *rest):
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX}:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and name == "messages":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return read_usage_rollup(conn, start=start, end=end, profile="default", **params)


def test_backfill_gap_merges_every_projection_and_preserves_event_counts(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        legacy_session(db, "old", 150)
        legacy_session(db, "outside", 99, tokens=999)
        legacy_session(db, "boundary", 200, tokens=999)
        legacy_session(db, "new", 250, tokens=0)
        set_coverage(db, 200)
        db.update_token_counts("new", model="claude-opus-5-5", billing_provider="anthropic",
                               input_tokens=50, api_call_count=1, usage_timestamp=260, turn_id="t")
    value = readonly_rollup(path, start=100, end=300)
    totals = value["totals"]
    assert totals["input_tokens"] == 150
    assert totals["total_tokens"] == 185
    assert totals["event_count"] == totals["turn_count"] == 1
    assert totals["unattributed_turn_events"] == 0
    assert totals["api_call_count"] == 3
    assert totals["estimated_cost_usd"] == pytest.approx(0.000829)
    assert value["chat_count"] == 2
    assert value["top_chats"][0]["title"] == "Fictional old"
    for key in ("agents", "models", "providers", "days", "top_chats"):
        for counter in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                        "reasoning_tokens", "api_call_count", "total_tokens", "event_count"):
            assert sum(row[counter] for row in value[key]) == totals[counter]
        assert sum(row["estimated_cost_usd"] for row in value[key]) == pytest.approx(totals["estimated_cost_usd"])
    assert value["backfill"] == {"since": 100, "until": 200, "basis": "session_totals_by_start", "session_count": 1}
    assert value["partial"] is False
    assert value["coverage_reason"] == "backfilled_from_session_totals"
    from tui_gateway.contracts.usage import UsageRollupWireResult
    assert UsageRollupWireResult.model_validate(value).root.backfill.session_count == 1
    old_server = {key: item for key, item in value.items() if key != "backfill"}
    assert UsageRollupWireResult.model_validate(old_server).root.backfill is None


def test_backfill_reconciles_main_switches_and_disjoint_auxiliaries_per_counter(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        legacy_session(db, "mixed", 150)
        db.update_token_counts("mixed", model="gpt-6.1-sol", billing_provider="openai-codex",
                               input_tokens=40, output_tokens=30, api_call_count=1, usage_timestamp=250)
        db.record_auxiliary_usage("mixed", "vision", model="claude-opus-5-5", billing_provider="anthropic",
                                  input_tokens=70, usage_events=[{"input_tokens": 30, "api_call_count": 1,
                                                               "usage_timestamp": 250}])
        db.record_auxiliary_usage("mixed", "title_generation", model="claude-opus-5-5",
                                  billing_provider="anthropic", input_tokens=11,
                                  usage_events=[{"input_tokens": 4, "api_call_count": 1, "usage_timestamp": 250}])
        # The main route table overlaps stored session counters and is not another source of totals.
        db._conn.execute("UPDATE sessions SET model='claude-opus-5-5', billing_provider='anthropic' WHERE id='mixed'")
        set_coverage(db, 200)
    value = readonly_rollup(path, start=100, end=300)
    assert value["totals"]["input_tokens"] == 221
    assert value["totals"]["output_tokens"] == 40
    models = {row["model"]: row for row in value["models"]}
    assert models["gpt-6.1-sol"]["input_tokens"] == 40
    assert models["claude-opus-5-5"]["input_tokens"] == 181
    assert value["totals"]["event_count"] == 3
    assert value["backfill"]["session_count"] == 1


def test_backfill_floors_each_counter_and_ignores_events_outside_window(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        legacy_session(db, "mixed", 150)
        db.update_token_counts("mixed", model="claude-opus-5-5", billing_provider="anthropic",
                               input_tokens=20, output_tokens=40, api_call_count=1, usage_timestamp=250)
        db.update_token_counts("mixed", input_tokens=7, api_call_count=1, usage_timestamp=90)
        db._conn.execute("UPDATE sessions SET input_tokens=100, output_tokens=10 WHERE id='mixed'")
        set_coverage(db, 200)
    value = readonly_rollup(path, start=100, end=300)
    assert value["totals"]["input_tokens"] == 100
    assert value["totals"]["output_tokens"] == 40
    assert value["totals"]["cache_read_tokens"] == 20
    assert value["totals"]["event_count"] == 1


def test_backfill_lineage_cohorts_branches_filters_limits_and_cycles(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        legacy_session(db, "root", 99, tokens=999)
        db.end_session("root", "compression")
        legacy_session(db, "tip", 120, parent="root")
        legacy_session(db, "child", 130, parent="tip", source="subagent")
        legacy_session(db, "metadata", 140, parent="tip", config={"_delegate_from": "tip"})
        legacy_session(db, "branch", 150, parent="root", config={"_branched_from": "root"})
        legacy_session(db, "cycle-a", 160, source="subagent")
        legacy_session(db, "cycle-b", 170, parent="cycle-a", source="subagent")
        db._conn.execute("UPDATE sessions SET parent_session_id='cycle-b' WHERE id='cycle-a'")
        set_coverage(db, 200)
    value = readonly_rollup(path, start=100, end=300, limit=1)
    assert value["chat_count"] == 3
    assert value["backfill"]["session_count"] == 6
    assert value["totals"]["input_tokens"] == 600
    assert value["top_chats"][0]["session_id"] == "root"
    assert value["top_chats"][0]["input_tokens"] == 300
    assert value["top_chats"][0]["title"] == "Fictional root"
    assert readonly_rollup(path, start=100, end=300, session_id="child")["totals"]["input_tokens"] == 300
    assert readonly_rollup(path, start=100, end=300, session_id="branch")["totals"]["input_tokens"] == 100
    assert readonly_rollup(path, start=100, end=300, session_id="cycle-b")["totals"]["input_tokens"] == 200


@pytest.mark.parametrize("since", [50, 100])
def test_backfill_not_needed_when_window_is_inside_collector_coverage(tmp_path, since):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        legacy_session(db, "not-backfilled", 150)
        set_coverage(db, since)
    value = readonly_rollup(path, start=100, end=300)
    assert value["totals"]["total_tokens"] == 0
    assert value.get("backfill") is None
    assert value["partial"] is False
    assert value["coverage_reason"] != "backfilled_from_session_totals"


@pytest.mark.parametrize("since", [None, 400])
def test_backfill_missing_or_future_coverage_keeps_partial_semantics(tmp_path, since):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        legacy_session(db, "old", 150)
        if since is None:
            db._conn.execute("DELETE FROM state_meta WHERE key='usage_events_since'")
            db._conn.commit()
        else:
            set_coverage(db, since)
    value = readonly_rollup(path, start=100, end=300)
    assert value["partial"] is True
    assert value.get("backfill") is None or value["backfill"]["until"] <= 300


def test_backfill_unknown_model_is_not_priced_as_zero(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        legacy_session(db, "unknown", 150, model="not-a-priced-model", provider="unknown")
        set_coverage(db, 200)
    value = readonly_rollup(path, start=100, end=300)
    assert value["totals"]["total_tokens"] == 135
    assert value["totals"]["estimated_cost_usd"] is None
    assert value["models"][0]["estimated_cost_usd"] is None
    assert value["totals"]["cost_status"] == "unknown"


@pytest.mark.parametrize("stamp,day", [
    ("2026-11-01T03:59:59+00:00", "2026-10-31"),
    ("2026-11-01T04:00:00+00:00", "2026-11-01"),
    ("2026-11-01T06:30:00+00:00", "2026-11-01"),
    ("2026-03-08T04:59:59+00:00", "2026-03-07"),
    ("2026-03-08T05:00:00+00:00", "2026-03-08"),
])
def test_backfill_day_uses_session_start_at_timezone_and_dst_midnight(tmp_path, stamp, day):
    from datetime import datetime
    path = tmp_path / "state.db"
    started = datetime.fromisoformat(stamp).timestamp()
    with SessionDB(db_path=path) as db:
        legacy_session(db, "old", started)
        set_coverage(db, started + 60)
    value = readonly_rollup(path, start=started - 1, end=started + 120, timezone_name="America/Toronto")
    assert [(row["day"], row["input_tokens"]) for row in value["days"]] == [(day, 100)]


def test_backfill_rpc_windows_and_profile_scope(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from hermes_cli import profiles
    midnight = datetime.now(ZoneInfo("America/Toronto")).replace(hour=0, minute=0, second=0, microsecond=0)
    secondary = tmp_path / "secondary"
    secondary.mkdir()
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: secondary if name == "secondary" else tmp_path)
    for home, tokens in [(tmp_path, 100), (secondary, 800)]:
        with SessionDB(db_path=home / "state.db") as db:
            legacy_session(db, "same-id", (midnight - timedelta(days=4)).timestamp(), tokens=tokens)
            legacy_session(db, "month-only", (midnight - timedelta(days=20)).timestamp(), tokens=50)
            set_coverage(db, (midnight - timedelta(days=2)).timestamp())
    week = rpc(window="7d", timezone="America/Toronto")["result"]
    month = rpc(window="30d", timezone="America/Toronto")["result"]
    assert week["totals"]["input_tokens"] == 100
    assert month["totals"]["input_tokens"] == 150
    other = rpc(window="7d", timezone="America/Toronto", profile="secondary")["result"]
    assert other["totals"]["input_tokens"] == 800
    assert other["agents"][0]["profile"] == "secondary"
    today = rpc(window="today", timezone="America/Toronto")["result"]
    assert today["totals"]["input_tokens"] == 0
    assert today.get("backfill") is None
    assert today["partial"] is False
    assert "backfill" not in rpc(window="all")["result"]
