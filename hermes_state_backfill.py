"""Session-start cohort residuals for the gap before timestamped collection."""
from hermes_state_alltime import session_counter_cte
from hermes_state_rollup import _COUNTERS


def backfill_usage_cte(*, session_id=None):
    """Subtract in-window events before assigning residuals to the recorded route."""
    event_sums = ", ".join(f"SUM({key}) AS {key}" for key in _COUNTERS)
    residuals = ", ".join(f"MAX(s.{key}-COALESCE(e.{key},0),0) AS {key}" for key in _COUNTERS)
    nonzero = "+".join(_COUNTERS) + ">0"
    # Main events can span routes, while the stored main total has only one recorded route.
    # Auxiliary task/route buckets are disjoint from that total and from one another.
    return session_counter_cte(session_id=session_id, cohort=True) + f""", recorded AS (
        SELECT session_id, task, CASE WHEN task='' THEN '' ELSE model END AS model,
            CASE WHEN task='' THEN '' ELSE provider END AS provider, {event_sums}
        FROM window_events WHERE session_id IN (SELECT session_id FROM counters)
        GROUP BY 1, 2, 3, 4
    ), residuals AS (
        SELECT s.session_id, s.chat_id, s.started_at, s.model, s.provider, s.task, {residuals}
        FROM counters s LEFT JOIN recorded e ON e.session_id=s.session_id AND e.task=s.task
            AND e.model=CASE WHEN s.task='' THEN '' ELSE s.model END
            AND e.provider=CASE WHEN s.task='' THEN '' ELSE s.provider END
    ), backfill AS MATERIALIZED (
        SELECT * FROM residuals WHERE {nonzero}
    ) """
