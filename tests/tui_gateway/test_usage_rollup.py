"""Windowed usage reads metadata only and never recharges absolute checkpoints."""
import sqlite3
import time

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
