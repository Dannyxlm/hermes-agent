"""The worker-ledger completion seam preserves finalized failures and concurrent child facts."""
import copy
import os

import pytest

from gateway.session_worker_delegation import persist_worker_delegation
from persistence import DelegationId
from persistence_assembly import open_persistence, profile_scope
from tests.persistence_sqlite.workers._support import EXECUTION, SESSION, ident, seed_store, worker_birth


@pytest.mark.parametrize("status", ["failed", "timeout", "interrupted"])
def test_worker_completion_prefers_finalized_runner_entry(tmp_path, status):
    store = open_persistence(profile_scope(home=tmp_path))
    try:
        epoch = seed_store(store)
        pid, birth = os.getpid(), worker_birth()
        delegation_id = DelegationId("unit")

        def persist(sequence, operation, payload):
            return persist_worker_delegation(store, **ident(epoch, sequence),
                                             operation=operation, payload=payload,
                                             worker_pid=pid, worker_birth=birth)

        route = dict(parent_session_id=SESSION, session_key="", origin_ui_session_id="", origin_session_id="")
        persist(1, "delegation.dispatch", dict(delegation_id=delegation_id, dispatched_at=1,
                                              task={"is_batch": True, "task_indexes": [0, 1]}, **route))
        raw = [{"task_index": i, "status": status, "summary": "raw output", "_child_role": "leaf",
                "_child_cost_usd": 0.1} for i in (0, 1)]
        final = [{"task_index": i, "status": status, "summary": "trimmed", "summary_truncated": True,
                  "live_transcript": f"/synthetic/{i}.jsonl"} for i in (0, 1)]
        for seq, entry in enumerate(raw, start=2):
            persist(seq, "delegation.child", {"delegation_id": delegation_id, "entry": entry})
        event = dict(type="async_delegation", delegation_id=delegation_id, status="error",
                     completed_at=2, results=final, **route)
        payload = {"event": event, "result": {"results": final}}
        original = copy.deepcopy(payload)
        persist(4, "delegation.complete", payload)
        assert payload == original
        row = store.coordination.get_delegation(delegation_id=delegation_id)
        assert row is not None and row.result is not None
        published = store.worker_executions.pending_delegation_event(
            execution_id=EXECUTION, session_id=SESSION, delegation_id=delegation_id)
        assert published is not None and published["results"] == final
        assert row.result["results"] == final
    finally:
        store.close()
