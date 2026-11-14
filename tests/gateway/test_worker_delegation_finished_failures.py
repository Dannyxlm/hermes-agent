"""The worker-ledger completion seam preserves finalized failures and concurrent child facts."""
import json
import sqlite3

import pytest

from gateway.session_worker_delegation import _child, _complete
from tools import async_delegation as ad


@pytest.mark.parametrize("status", ["failed", "timeout", "interrupted"])
def test_worker_completion_prefers_finalized_runner_entry(tmp_path, status):
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.row_factory = sqlite3.Row
    try:
        ad._initialize_schema(conn)
        conn.execute("""INSERT INTO async_delegations
            (delegation_id,origin_session,parent_session_id,owner_execution_id,state,dispatched_at,updated_at,task_json)
            VALUES('unit','','parent','worker','running',1,1,?)""",
            (json.dumps({"is_batch": True, "task_indexes": [0, 1]}),))
        raw = [{"task_index": i, "status": status, "summary": "raw output", "_child_role": "leaf",
                "_child_cost_usd": 0.1} for i in (0, 1)]
        final = [{"task_index": i, "status": status, "summary": "trimmed", "summary_truncated": True,
                  "live_transcript": f"/synthetic/{i}.jsonl"} for i in (0, 1)]
        for entry in raw:
            _child(None, conn, "parent", {"delegation_id": "unit", "entry": entry}, execution_id="worker")
        event = {"type": "async_delegation", "delegation_id": "unit", "parent_session_id": "parent",
                 "status": "error", "completed_at": 2, "results": final}
        payload = {"event": event, "result": {"results": final}}
        original = json.dumps(payload, sort_keys=True)
        _complete(None, conn, "parent", payload, execution_id="worker")
        assert json.dumps(payload, sort_keys=True) == original
        row = conn.execute("SELECT event_json,result_json FROM async_delegations").fetchone()
        assert json.loads(row[0])["results"] == final
        assert json.loads(row[1])["results"] == final
    finally:
        conn.close()
