"""Usage totals must include the auxiliary spend the per-model rows already show.

``/api/analytics/usage`` folds auxiliary usage (background review, compression,
title generation, vision, ...) into ``by_model`` (#23270), because aux calls never
touch the ``sessions`` counters. ``totals`` and ``daily`` were still summed from
``sessions`` alone, so the per-model rows added up to more than the stated total:
on the live 30-day window the first model row was larger than the total
(Hermex Usage screen, Gate C G-4). The same gap existed between the Models page
cards and its header totals.
"""

import time

import pytest

from hermes_cli.web_routers import analytics as web_server
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


@pytest.fixture
def run(monkeypatch, db):
    monkeypatch.setattr(web_server, "_open_session_db_for_profile", lambda profile, read_only=True: db)
    monkeypatch.setattr(web_server, "_usage_cache_for", lambda profile: None)
    monkeypatch.setattr(db, "close", lambda: None)
    return db


def _seed(db):
    """Two costed sessions, one with background-review aux spend, one aux-only model."""
    db.create_session("s1", source="cli", model="claude-opus-5-5")
    db.update_token_counts("s1", input_tokens=1_000, output_tokens=200, cache_read_tokens=5_000,
                           estimated_cost_usd=3.0, model="claude-opus-5-5",
                           billing_provider="anthropic", api_call_count=2)
    db.record_auxiliary_usage("s1", "background_review", model="claude-opus-5-5",
                              billing_provider="anthropic", input_tokens=300, output_tokens=40,
                              cache_read_tokens=700, estimated_cost_usd=0.5)
    db.create_session("s2", source="cli", model="gpt-6-astra")
    db.update_token_counts("s2", input_tokens=4_000, output_tokens=100, model="gpt-6-astra",
                           billing_provider="openai-codex", api_call_count=1)
    db.record_auxiliary_usage("s2", "title_generation", model="gpt-6-luna",
                              billing_provider="openai-codex", input_tokens=60, output_tokens=6,
                              estimated_cost_usd=0.25)


def test_usage_rows_sum_to_totals(run):
    _seed(run)
    data = web_server._get_usage_analytics(days=30)
    totals, rows = data["totals"], data["by_model"]

    assert sum(r["estimated_cost"] for r in rows) == pytest.approx(totals["total_estimated_cost"])
    assert totals["total_estimated_cost"] == pytest.approx(3.75)
    # No single row can exceed the total it is a share of.
    assert max(r["estimated_cost"] for r in rows) <= totals["total_estimated_cost"]
    assert sum(r["input_tokens"] for r in rows) == totals["total_input"] == 5_360
    assert sum(r["output_tokens"] for r in rows) == totals["total_output"] == 346
    assert totals["total_cache_read"] == 5_700
    assert sum(r["api_calls"] for r in rows) == totals["total_api_calls"] == 5
    # Aux calls happen inside sessions already counted; the session count is unchanged.
    assert totals["total_sessions"] == 2


def test_usage_daily_sums_to_totals(run):
    _seed(run)
    data = web_server._get_usage_analytics(days=30)
    totals, daily = data["totals"], data["daily"]

    assert sum(d["estimated_cost"] for d in daily) == pytest.approx(totals["total_estimated_cost"])
    assert sum(d["estimated_cost"] for d in daily) == pytest.approx(3.75)
    assert sum(d["input_tokens"] for d in daily) == totals["total_input"]
    assert sum(d["output_tokens"] for d in daily) == totals["total_output"]
    assert sum(d["sessions"] for d in daily) == totals["total_sessions"]


def test_aux_outside_window_is_excluded(run):
    _seed(run)
    run.create_session("old", source="cli", model="claude-opus-5-5")
    run.record_auxiliary_usage("old", "compression", model="claude-opus-5-5",
                               input_tokens=9_999, output_tokens=9_999, estimated_cost_usd=99.0)
    with run._lock:
        run._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 'old'", (time.time() - 40 * 86400,))
        run._conn.commit()

    data = web_server._get_usage_analytics(days=30)
    assert data["totals"]["total_estimated_cost"] == pytest.approx(3.75)
    assert sum(r["estimated_cost"] for r in data["by_model"]) == pytest.approx(3.75)


def test_no_aux_leaves_totals_unchanged(run):
    run.create_session("s1", source="cli", model="m")
    run.update_token_counts("s1", input_tokens=10, output_tokens=2, estimated_cost_usd=0.1, model="m")
    totals = web_server._get_usage_analytics(days=30)["totals"]
    assert totals["total_input"] == 10 and totals["total_output"] == 2
    assert totals["total_estimated_cost"] == pytest.approx(0.1)


def test_models_header_totals_include_aux(run):
    _seed(run)
    data = web_server._get_models_analytics(days=30)
    cards, totals = data["models"], data["totals"]

    assert sum(c["estimated_cost"] for c in cards) == pytest.approx(totals["total_estimated_cost"])
    assert sum(c["input_tokens"] for c in cards) == totals["total_input"]
    assert sum(c["output_tokens"] for c in cards) == totals["total_output"]
    assert totals["total_sessions"] == 2
