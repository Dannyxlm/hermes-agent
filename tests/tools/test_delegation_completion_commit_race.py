"""Forced completion must preserve children committed after its in-memory snapshot."""
import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.mark.parametrize("status", ["stalled", "error"])
def test_committed_child_beats_forced_completion_placeholder(monkeypatch, status):
    ad._reset_for_tests()
    offered = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", offered)
    did = "commit-race"
    record = dict(delegation_id=did, dispatched_at=time.time(), status="running", is_batch=True,
                  goals=["first", "second", "unfinished"], task_indexes=[3, 7, 9], session_key="owner")
    ad._persist_dispatch(record)
    with ad._records_lock:
        ad._records[did] = record
    children = [dict(task_index=i, status="completed", summary=f"answer {i}") for i in (3, 7)]
    ad.record_unit_child(did, children[0])
    entered, release = threading.Event(), threading.Event()
    persist = ad._persist_completion

    def paused(event, result):
        entered.set()
        assert release.wait(5)
        return persist(event, result)

    monkeypatch.setattr(ad, "_persist_completion", paused)
    result = (lambda rec: ad._stalled_result(did, rec)) if status == "stalled" else ad._batch_crash("boom", 1.0)
    worker = threading.Thread(target=ad._finalize, args=(did, result, status))
    worker.start()
    try:
        assert entered.wait(5)
        ad.record_unit_child(did, children[1])
    finally:
        release.set()
        worker.join(5)
    try:
        assert not worker.is_alive()
        event = offered.get_nowait()
        assert event["results"][:2] == children
        assert event["results"][2]["status"] == ("unknown" if status == "stalled" else "error")
        assert ad.get_durable_delegation(did)["result"]["results"] == event["results"]
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 1
        assert replay.get_nowait()["results"] == event["results"]
    finally:
        ad._reset_for_tests()
