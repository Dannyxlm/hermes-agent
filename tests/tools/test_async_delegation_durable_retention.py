"""Delivery-pending results are not completed history until settled or explicitly dropped."""

import json
import queue
import time

import pytest

from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def clean_state():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _seed(conn, delegation_id, *, delivery="pending", state="completed", updated_at=None):
    now = time.time() if updated_at is None else updated_at
    event = {"type": "async_delegation", "delegation_id": delegation_id, "status": state,
             "completed_at": now, "summary": "retained result"}
    conn.execute("""INSERT INTO async_delegations
        (delegation_id, origin_session, state, dispatched_at, completed_at, updated_at,
         delivery_state, event_json, result_json)
        VALUES (?, 'owner', ?, ?, ?, ?, ?, ?, ?)""",
        (delegation_id, state, now, now, now, delivery, json.dumps(event),
         json.dumps({"summary": "retained result"})))


def test_dispatch_prunes_settled_history_without_losing_pending_replay():
    pending_ids = {f"pending-{i}" for i in range(ad._MAX_RETAINED_COMPLETED + 1)}
    now = time.time()
    with ad._transaction() as conn:
        for delegation_id in pending_ids:
            _seed(conn, delegation_id, updated_at=now - 100)
        for i in range(ad._MAX_RETAINED_COMPLETED + 1):
            _seed(conn, f"history-{i}", delivery="delivered" if i % 2 else "dropped",
                  updated_at=now + i)

    # Dispatch is the production retention boundary; completion payloads must remain replayable.
    ad._persist_dispatch({"delegation_id": "next", "dispatched_at": now})

    with ad._transaction() as conn:
        pending = {row[0] for row in conn.execute(
            "SELECT delegation_id FROM async_delegations WHERE state='completed' AND delivery_state='pending'")}
        history = {row[0] for row in conn.execute(
            "SELECT delegation_id FROM async_delegations WHERE delivery_state IN ('delivered','dropped')")}
    assert pending == pending_ids
    assert len(history) == ad._MAX_RETAINED_COMPLETED
    assert "history-0" not in history
    offered = queue.Queue()
    assert ad.restore_undelivered_completions(offered) == len(pending_ids)
    assert {offered.get_nowait()["delegation_id"] for _ in pending_ids} == pending_ids
    assert ad.claim_completion_delivery("pending-0", "first")
    assert not ad.claim_completion_delivery("pending-0", "duplicate")


def test_pending_overflow_records_drop_before_shedding_delivery(monkeypatch, caplog):
    monkeypatch.setattr(ad, "_MAX_DURABLE_PENDING", 2)
    now = time.time()
    with ad._transaction() as conn:
        for i in range(3):
            _seed(conn, f"pending-{i}", updated_at=now + i)
        conn.execute("""UPDATE async_delegations SET delivery_claim='in-flight', delivery_claimed_at=?
                        WHERE delegation_id='pending-0'""", (now,))

    ad._prune_durable_records()

    dropped = ad.get_durable_delegation("pending-0")
    assert dropped is not None
    assert dropped["delivery_state"] == "dropped"
    assert dropped["result"]["summary"] == "retained result"
    assert not ad.claim_completion_delivery("pending-0", "late-queued-copy")
    assert not ad.complete_completion_delivery("pending-0", "in-flight")
    with ad._transaction() as conn:
        assert conn.execute("""SELECT delivery_claim, delivery_claimed_at FROM async_delegations
                               WHERE delegation_id='pending-0'""").fetchone() == (None, None)
    assert all(ad.get_durable_delegation(f"pending-{i}")["delivery_state"] == "pending" for i in (1, 2))
    assert "pending retention limit" in caplog.text
    offered = queue.Queue()
    assert ad.restore_undelivered_completions(offered) == 2
    assert {offered.get_nowait()["delegation_id"] for _ in range(2)} == {"pending-1", "pending-2"}


def test_expired_delivered_and_dropped_history_is_pruned_but_pending_and_live_remain():
    old = time.time() - ad._DURABLE_RETENTION_SECONDS - 1
    with ad._transaction() as conn:
        _seed(conn, "old-delivered", delivery="delivered", updated_at=old)
        _seed(conn, "old-dropped", delivery="dropped", updated_at=old)
        _seed(conn, "old-pending", updated_at=old)
        _seed(conn, "running", state="running", updated_at=old)
        _seed(conn, "finalizing", state="finalizing", updated_at=old)

    ad._prune_durable_records()

    with ad._transaction() as conn:
        assert {row[0] for row in conn.execute("SELECT delegation_id FROM async_delegations")} == {
            "old-pending", "running", "finalizing"}
