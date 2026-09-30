"""Deliberate ``hermes auth priority`` ordering must survive a stale routine pool flush.

Two live processes share ``auth.json``. Process A loads the pool, then the operator reorders it
(process B: ``move_entry``), then process A performs an ordinary flush (rotation/cooldown) from
its stale in-memory snapshot. Without ordering arbitration the stale flush writes the old
priorities straight back over the operator's reorder.
"""
from __future__ import annotations

import time

import pytest

from agent.credential_pool import CredentialPool, PooledCredential, load_pool
from hermes_cli.auth import (
    PoolOrderState, read_credential_pool, read_pool_order_generation, write_credential_pool,
)

PROVIDER = "anthropic"


@pytest.fixture(autouse=True)
def isolated_auth_store(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def _rows():
    return [
        dict(id="danny1", label="DANNY-ANT", source="manual:hermes_pkce", auth_type="oauth",
             access_token="fixture-access-danny", refresh_token="fixture-refresh-danny", priority=0,
             last_status="ok", last_status_at=time.time()),
        dict(id="sj0001", label="SJ-ANT", source="manual:hermes_pkce", auth_type="oauth",
             access_token="fixture-access-sj", refresh_token="fixture-refresh-sj", priority=1,
             last_status="ok", last_status_at=time.time()),
    ]


def _order():
    return [(r["label"], r["priority"]) for r in sorted(read_credential_pool(PROVIDER), key=lambda r: r["priority"])]


def _seed():
    rows = [PooledCredential.from_dict(PROVIDER, r).to_dict() for r in _rows()]
    write_credential_pool(PROVIDER, rows)


def test_stale_flush_after_operator_reorder_keeps_new_order():
    _seed()
    stale = load_pool(PROVIDER)                      # process A snapshot: DANNY first
    assert [e.label for e in stale._entries] == ["DANNY-ANT", "SJ-ANT"]

    operator = load_pool(PROVIDER)                   # process B: operator moves SJ first
    moved = operator.move_entry("sj0001", 0)
    assert moved is not None and moved.priority == 0
    assert _order() == [("SJ-ANT", 0), ("DANNY-ANT", 1)]
    assert read_pool_order_generation(PROVIDER) == 1

    stale._persist()                                 # process A routine flush from old snapshot
    assert _order() == [("SJ-ANT", 0), ("DANNY-ANT", 1)], "stale flush must not revert the reorder"
    # Process A's in-memory order follows the operator too, so its next fill_first pick agrees.
    assert [e.label for e in stale._entries] == ["SJ-ANT", "DANNY-ANT"]
    assert stale._order_generation == 1


def test_reorder_after_stale_flush_still_wins():
    """Order of events reversed: the reorder lands AFTER the routine flush and is simply newer."""
    _seed()
    live = load_pool(PROVIDER)
    live._persist()
    operator = load_pool(PROVIDER)
    operator.move_entry("sj0001", 0)
    assert _order() == [("SJ-ANT", 0), ("DANNY-ANT", 1)]
    live._persist()                                  # second stale flush, still behind generation 1
    assert _order() == [("SJ-ANT", 0), ("DANNY-ANT", 1)]


def test_routine_flush_without_reorder_is_unchanged():
    """No deliberate reorder ever happened: legacy behaviour (memory order persists) is kept."""
    _seed()
    live = load_pool(PROVIDER)
    live._persist()
    assert _order() == [("DANNY-ANT", 0), ("SJ-ANT", 1)]
    assert read_pool_order_generation(PROVIDER) == 0


def test_two_deliberate_reorders_are_monotonic():
    _seed()
    a = load_pool(PROVIDER)
    b = load_pool(PROVIDER)
    a.move_entry("sj0001", 0)
    assert read_pool_order_generation(PROVIDER) == 1
    b.move_entry("danny1", 0)                        # b was at generation 0 but is a deliberate reorder
    assert read_pool_order_generation(PROVIDER) == 2
    assert _order() == [("DANNY-ANT", 0), ("SJ-ANT", 1)]
    a._persist()                                     # a is at generation 1 < 2: adopts b's order
    assert _order() == [("DANNY-ANT", 0), ("SJ-ANT", 1)]
    assert [e.label for e in a._entries] == ["DANNY-ANT", "SJ-ANT"]


def test_arbitration_only_touches_priority_and_is_provider_scoped():
    """Token generation / cooldown merges are untouched; another provider's generation is separate."""
    _seed()
    other = [PooledCredential.from_dict("xai-oauth", dict(
        id="grok01", label="SHANNON-GROK", source="manual:device_code", auth_type="oauth",
        access_token="fixture-grok", refresh_token="fixture-grok-r", priority=0)).to_dict()]
    write_credential_pool("xai-oauth", other)
    stale = load_pool(PROVIDER)
    load_pool(PROVIDER).move_entry("sj0001", 0)
    assert read_pool_order_generation("xai-oauth") == 0
    # Stale process also marks DANNY exhausted in memory; that observation must still land.
    danny = next(e for e in stale._entries if e.id == "danny1")
    from dataclasses import replace
    stale._replace_entry(danny, replace(danny, last_status="exhausted", last_status_at=time.time(),
                                        last_error_code=429, last_error_reset_at=time.time() + 600))
    stale._persist()
    rows = {r["id"]: r for r in read_credential_pool(PROVIDER)}
    assert rows["danny1"]["last_status"] == "exhausted"
    assert rows["danny1"]["priority"] == 1 and rows["sj0001"]["priority"] == 0
    assert rows["danny1"]["access_token"] == "fixture-access-danny"


def test_write_credential_pool_without_order_is_legacy():
    _seed()
    rows = read_credential_pool(PROVIDER)
    for r in rows:
        r["priority"] = 1 - r["priority"]
    write_credential_pool(PROVIDER, rows)            # no order= -> last writer wins, as before
    assert _order() == [("SJ-ANT", 0), ("DANNY-ANT", 1)]
    state = PoolOrderState(0)
    write_credential_pool(PROVIDER, rows, order=state)
    assert state.generation == 0 and state.adopted_disk_order is False
