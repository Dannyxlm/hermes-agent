"""Session-start cohort residuals for the gap before timestamped collection."""
from hermes_state_alltime import session_counter_cte
from hermes_state_rollup import _COUNTERS
from hermes_state_usage import AUXILIARY_USAGE_TASKS


def backfill_usage_cte(*, session_id=None):
    """Subtract in-window events before assigning residuals to the recorded route."""
    event_sums = ", ".join(f"SUM({key}) AS {key}" for key in _COUNTERS)
    residuals = ", ".join(f"MAX(s.{key}-COALESCE(e.{key},0),0) AS {key}" for key in _COUNTERS)
    nonzero = "+".join(_COUNTERS) + ">0"
    aux_tasks = ", ".join("'" + task + "'" for task in sorted(AUXILIARY_USAGE_TASKS))
    # Per-turn routes belong to the stored main total, not to disjoint auxiliary buckets.
    return session_counter_cte(session_id=session_id, cohort=True) + f""", recorded AS (
        SELECT session_id, CASE WHEN task IN ({aux_tasks}) THEN task ELSE '' END AS task,
            CASE WHEN task IN ({aux_tasks}) THEN model ELSE '' END AS model,
            CASE WHEN task IN ({aux_tasks}) THEN provider ELSE '' END AS provider, {event_sums}
        FROM window_events WHERE session_id IN (SELECT session_id FROM counters)
        GROUP BY 1, 2, 3, 4
    ), stored AS (
        SELECT session_id, chat_id, started_at, model, provider, task, {event_sums}
        FROM counters GROUP BY session_id, task, model, provider
    ), residuals AS (
        SELECT s.session_id, s.chat_id, s.started_at, s.model, s.provider, s.task, {residuals}
        FROM stored s LEFT JOIN recorded e ON e.session_id=s.session_id AND e.task=s.task
            AND e.model=CASE WHEN s.task='' THEN '' ELSE s.model END
            AND e.provider=CASE WHEN s.task='' THEN '' ELSE s.provider END
    ), backfill AS MATERIALIZED (
        SELECT * FROM residuals WHERE {nonzero}
    ) """
