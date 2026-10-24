"""Worker receipts carry the fork's immutable per-call accounting identity."""
from contextlib import closing

import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt


def test_worker_usage_preserves_turn_and_original_event_timestamp_once(tmp_path):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        db.create_session("s", source="cli")
        scope = dict(epoch=rt.begin_runtime_epoch(db, instance_id="owner"),
                     execution_id="w", session_id="s", generation=0)
        rt.register_worker_execution(db, **scope, kind="compute", adoption_secret="fixture")
        args = dict(**scope, sequence=1, operation="usage.main", payload={
            "model": "m", "input_tokens": 13, "api_call_count": 1,
            "turn_id": "turn", "usage_timestamp": 123.5,
        })
        assert rt.mutate_worker_execution(db, **args) == rt.mutate_worker_execution(db, **args)
        with db._read_ctx() as conn:
            rows = [tuple(row) for row in conn.execute(
                "SELECT turn_id, occurred_at, input_tokens FROM usage_events WHERE session_id='s'")]
        assert rows == [("turn", 123.5, 13)]


@pytest.mark.parametrize("payload", [
    {"usage_timestamp": float("inf")}, {"usage_timestamp": -1},
    {"turn_id": {}}, {"usage_events": [{"input_tokens": -1}]},
    {"usage_events": [{"usage_events": []}]}, {"unrecognized": True},
])
def test_worker_usage_metadata_remains_a_closed_validated_boundary(payload):
    from hermes_state_worker_usage import validate_usage
    with pytest.raises(rt.RuntimeStoreError, match="invalid_params"):
        validate_usage(payload, auxiliary=False)
