"""Usage preserves measured cache/cost values and unknown-cost provenance."""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from gateway.session_usage import tui_usage, usage_stats


@pytest.fixture(params=[server._get_usage, lambda agent: tui_usage(usage_stats(agent))],
                ids=["embedded", "canonical"])
def usage_for(request):
    return request.param


@pytest.mark.parametrize("estimated,actual,status,expected", [
    (0.125, None, "estimated", 0.125),
    (0.125, 0.0, "actual", 0.0),
    (0.0, None, "unknown", None),
    (0.0, None, "included", 0.0),
])
def test_usage_cache_cost_from_agent(estimated, actual, status, expected, usage_for):
    agent = SimpleNamespace(model="fixture", session_cache_read_tokens=80,
                            session_cache_write_tokens=0, session_estimated_cost_usd=estimated,
                            session_actual_cost_usd=actual, session_cost_status=status)
    usage = usage_for(agent)
    assert (usage["cache_read"], usage["cache_write"]) == (80, 0)
    assert usage["cost_status"] == status
    if expected is None:
        assert "cost_usd" not in usage
    else:
        assert usage["cost_usd"] == expected


def test_usage_unknown_fields_are_omitted(usage_for):
    usage = usage_for(SimpleNamespace())
    assert not {"cache_read", "cache_write", "cost_usd"} & usage.keys()
    assert usage["cost_status"] == "unknown"


def test_usage_persisted_actual_cost_overrides_agent_estimate(tmp_path, usage_for):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("cost-session", "desktop")
        db._conn.execute("UPDATE sessions SET estimated_cost_usd=0.2, actual_cost_usd=0.15, "
                         "cost_status='actual' WHERE id='cost-session'")
        agent = SimpleNamespace(_session_db=db, session_id="cost-session",
                                session_estimated_cost_usd=0.2, session_cost_status="estimated")
        usage = usage_for(agent)
        assert usage["cost_usd"] == 0.15
        assert usage["cost_status"] == "actual"