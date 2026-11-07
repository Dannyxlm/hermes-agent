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


def test_worker_owned_settlement_preserves_committed_partial_children(tmp_path):
    from gateway.session_worker_delegation import _child, _complete
    from hermes_state import SessionDB

    task = {'is_batch': True, 'goals': ['first', 'second'], 'task_indexes': [3, 7]}
    child = {'task_index': 3, 'status': 'completed', 'summary': 'durable useful work'}
    event = {'type': 'async_delegation', 'delegation_id': 'worker-partial', 'status': 'stalled',
             'completed_at': time.time(), 'parent_session_id': 'parent', 'session_key': 'owner',
             'origin_ui_session_id': '', 'origin_session_id': '', 'error': 'unfinished child'}
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('parent', source='cli')
        db._write_sql('''INSERT INTO async_delegations
            (delegation_id,origin_session,parent_session_id,state,dispatched_at,updated_at,
             task_json,owner_execution_id) VALUES(?,?,?,'running',?,?,?,?)''',
            ('worker-partial', 'owner', 'parent', time.time(), time.time(), json.dumps(task), 'worker'))
        db._execute_write(lambda conn: _child(db, conn, 'parent',
            {'delegation_id': 'worker-partial', 'entry': child}, execution_id='worker'))
        db._execute_write(lambda conn: _complete(db, conn, 'parent',
            {'event': event, 'result': {'status': 'stalled'}}, execution_id='worker'))
        with db._read_ctx() as conn:
            row = conn.execute('SELECT event_json,result_json FROM async_delegations').fetchone()
        children = json.loads(row['result_json']).get('results', [])
        assert children and children[0] == child, 'worker-owned completion erased a committed child'
        assert children[1]['task_index'] == 7 and children[1]['status'] == 'unknown'
        assert json.loads(row['event_json'])['results'] == children


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


@pytest.mark.parametrize('commit_during_finalize', [False, True])
def test_forced_settlement_preserves_committed_children_on_delivery_and_reload(monkeypatch, commit_during_finalize):
    import threading
    from tools.process_registry import process_registry

    offered = queue.Queue()
    monkeypatch.setattr(process_registry, 'completion_queue', offered)
    record = dict(delegation_id='partial-batch', dispatched_at=time.time(), status='running',
                  is_batch=True, goals=['finished', 'unfinished'], task_indexes=[3, 7], session_key='owner')
    ad._persist_dispatch(record)
    with ad._records_lock:
        ad._records['partial-batch'] = record
    completed = dict(task_index=3, status='completed', summary='durable useful work')
    entered, release = threading.Event(), threading.Event()
    persist = ad._persist_completion

    def paused_persist(event, result):
        entered.set()
        assert release.wait(5)
        return persist(event, result)

    if commit_during_finalize:
        monkeypatch.setattr(ad, '_persist_completion', paused_persist)
    else:
        ad.record_unit_child('partial-batch', completed)
        release.set()
    worker = threading.Thread(target=ad._finalize,
                              args=('partial-batch', lambda rec: ad._stalled_result('partial-batch', rec), 'stalled'))
    worker.start()
    try:
        if commit_during_finalize:
            assert entered.wait(5)
            ad.record_unit_child('partial-batch', completed)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    event = offered.get_nowait()
    expected = [completed, dict(task_index=7, status='unknown', summary=None, error=event['error'])]
    assert event['results'] == expected
    assert 'run it again' not in event['error']
    assert ad.get_durable_delegation('partial-batch')['result']['results'] == expected
    # A late runner's empty result cannot replace terminal delivery.
    ad._finalize('partial-batch', {'results': []}, 'completed')
    assert offered.empty()
    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 1
    assert replay.get_nowait()['results'] == expected


def test_forced_settlement_with_every_child_recorded_has_no_unknown_children(monkeypatch):
    from tools.process_registry import process_registry
    offered = queue.Queue()
    monkeypatch.setattr(process_registry, 'completion_queue', offered)
    record = dict(delegation_id='finished-batch', dispatched_at=time.time(), status='running',
                  is_batch=True, goals=['first', 'second'], session_key='owner')
    ad._persist_dispatch(record)
    with ad._records_lock:
        ad._records['finished-batch'] = record
    children = [dict(task_index=i, status='completed', summary=f'answer {i}') for i in range(2)]
    for child in reversed(children):
        ad.record_unit_child('finished-batch', child)
    ad._finalize('finished-batch', lambda rec: ad._stalled_result('finished-batch', rec), 'stalled')
    assert offered.get_nowait()['results'] == children
    assert ad.get_durable_delegation('finished-batch')['result']['results'] == children
