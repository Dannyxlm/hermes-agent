"""SQL rollups of retained accounting counters, without request-history inference."""
import json
from functools import lru_cache

from agent.api_value_pricing import estimate_aggregate_api_value_decimal, get_api_value_rates
from agent.usage_pricing import CanonicalUsage
from hermes_state_rollup import _COUNTERS, _SUMS, _DecimalSum, _metrics, follows_usage_parent
from hermes_state_usage import AUXILIARY_USAGE_TASKS

_SESSION_COUNTERS = """WITH RECURSIVE routes AS MATERIALIZED (
    SELECT s.id, CASE WHEN p.id IS NOT NULL
        AND usage_follows_parent(s.source, s.model_config, p.end_reason)
        THEN p.id END AS parent_id
    FROM sessions s LEFT JOIN sessions p ON p.id=s.parent_session_id
), lineage(origin, id) AS (
    SELECT id, id FROM routes {lineage_filter}
    UNION
    SELECT l.origin, r.parent_id FROM lineage l JOIN routes r ON r.id=l.id
    WHERE r.parent_id IS NOT NULL
), roots AS (
    SELECT l.origin, COALESCE(MIN(CASE WHEN r.parent_id IS NULL THEN l.id END), MIN(l.id)) AS chat_id
    FROM lineage l JOIN routes r ON r.id=l.id GROUP BY l.origin
), counters AS (
    SELECT s.id AS session_id, r.chat_id, s.started_at, COALESCE(s.model, '') AS model,
        COALESCE(s.billing_provider, '') AS provider, '' AS task, {session_counters}
    FROM sessions s {session_join} roots r ON r.origin=s.id
    WHERE ({session_nonzero}) {chat_filter} {cohort_filter}
    UNION ALL
    SELECT s.id AS session_id, r.chat_id, s.started_at, COALESCE(a.model, '') AS model,
        COALESCE(a.billing_provider, '') AS provider, a.task, {aux_counters}
    FROM session_model_usage a JOIN sessions s ON s.id=a.session_id JOIN roots r ON r.origin=s.id
    WHERE a.task IN ({aux_tasks}) AND ({aux_nonzero}) {chat_filter} {cohort_filter}
) """
_PRICED_USAGE = """, priced AS MATERIALIZED (
    SELECT *, session_api_value(model, provider, {arguments}) AS price FROM counters
), usage AS (
    SELECT *, json_extract(price, '$.estimated_cost_usd') AS estimated_cost_usd,
        json_extract(price, '$.minimum_estimated_cost_usd') AS minimum_estimated_cost_usd,
        json_extract(price, '$.reason') AS price_reason,
        json_extract(price, '$.pricing_date') AS pricing_date,
        json_extract(price, '$.source_url') AS source_url FROM priced
) """
_AGGREGATES = (_SUMS + ", NULL AS turn_count, NULL AS event_count, NULL AS unattributed_turn_events, "
               "decimal_sum(e.estimated_cost_usd) AS known_estimated_cost_usd, "
               "decimal_sum(e.minimum_estimated_cost_usd) AS minimum_estimated_cost_usd, "
               "SUM(e.estimated_cost_usd IS NULL) AS unknown_price_events, "
               "SUM(e.estimated_cost_usd IS NULL AND e.minimum_estimated_cost_usd IS NOT NULL) AS minimum_price_events, "
               "GROUP_CONCAT(DISTINCT e.price_reason) AS cost_reason, "
               "GROUP_CONCAT(DISTINCT e.pricing_date) AS pricing_dates, "
               "GROUP_CONCAT(DISTINCT e.source_url) AS price_sources")
_TOTAL_TOKENS = "+".join(f"e.{key}" for key in _COUNTERS[:4])


def _session_value(model, provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, api_call_count):
    usage = CanonicalUsage(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens)
    value = estimate_aggregate_api_value_decimal(model, usage, provider=provider, api_call_count=api_call_count,
        tier_reason="per-chat totals: original call boundaries unavailable for context-tier pricing")
    return json.dumps(value, default=str)


def _session_metrics(row):
    result = _metrics(row)
    result.update(turn_count=None, event_count=None, unattributed_turn_events=None)
    return result


def session_counter_cte(*, session_id=None, cohort=False):
    """Recorded main route plus disjoint named auxiliaries, with cycle-safe chat ownership."""
    return _SESSION_COUNTERS.format(
        session_counters=", ".join(f"COALESCE(s.{key},0) AS {key}" for key in _COUNTERS),
        aux_counters=", ".join(f"COALESCE(a.{key},0) AS {key}" for key in _COUNTERS),
        session_nonzero="+".join(f"COALESCE(s.{key},0)" for key in _COUNTERS) + ">0",
        aux_nonzero="+".join(f"COALESCE(a.{key},0)" for key in _COUNTERS) + ">0",
        aux_tasks=", ".join("'" + task + "'" for task in sorted(AUXILIARY_USAGE_TASKS)),
        # Keep the indexed start cohort outside the lineage join instead of probing every session.
        session_join="CROSS JOIN" if cohort else "JOIN",
        lineage_filter=("WHERE id IN (SELECT id FROM sessions WHERE started_at>=:start "
                        "AND started_at<:coverage_since) OR id=:session_id") if cohort else "",
        chat_filter="AND r.chat_id=(SELECT chat_id FROM roots WHERE origin=:session_id)" if session_id else "",
        cohort_filter="AND s.started_at>=:start AND s.started_at<:coverage_since" if cohort else "")


def read_alltime_usage(conn, *, end, profile, session_id=None, limit=20, timezone_name="UTC"):
    """Counted session and disjoint auxiliary usage in one read snapshot."""
    cached_value = lru_cache(maxsize=8192)(_session_value)
    conn.create_function("session_api_value", 7, cached_value, deterministic=True)
    conn.create_function("usage_follows_parent", 3, follows_usage_parent, deterministic=True)
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    cte = session_counter_cte(session_id=session_id) + _PRICED_USAGE.format(
        arguments=", ".join(_COUNTERS[:4]) + ", api_call_count")
    args = {"session_id": session_id}
    base = cte + "SELECT {columns}" + _AGGREGATES + " FROM usage e {suffix}"
    totals_row = conn.execute(base.format(
        columns="MIN(e.started_at) AS since, COUNT(DISTINCT e.chat_id) AS chat_count, ", suffix=""), args).fetchone()
    since, chat_count = totals_row["since"], totals_row["chat_count"]
    totals = _session_metrics({key: totals_row[key] for key in totals_row.keys() if key not in {"since", "chat_count"}})
    def groups(columns, group, order="total_tokens DESC", extra_args=None):
        return [_session_metrics(row) for row in conn.execute(base.format(
            columns=f"{columns}, SUM({_TOTAL_TOKENS}) AS total_tokens, ",
            suffix=f"GROUP BY {group} ORDER BY {order}"), {**args, **(extra_args or {})})]
    models = groups("e.model, e.provider", "e.model, e.provider", "total_tokens DESC, e.model, e.provider")
    for model in models:
        model["rates"] = get_api_value_rates(model["model"], provider=model["provider"])
    providers = groups("e.provider", "e.provider", "total_tokens DESC, e.provider")
    chats = groups("e.chat_id AS session_id, (SELECT title FROM sessions WHERE id=e.chat_id) AS title",
                   "e.chat_id", "total_tokens DESC, e.chat_id LIMIT :limit", {"limit": limit})
    coverage = [dict(row) for row in conn.execute(
        cte + "SELECT provider, MIN(started_at) AS since, COUNT(DISTINCT chat_id) AS chat_count, "
        "COUNT(DISTINCT session_id) AS session_count FROM counters GROUP BY provider ORDER BY provider", args)]
    return {"profile": profile, "window": "all", "source": "session_totals",
            "start": since, "end": end, "timezone": timezone_name, "totals": totals,
            "agents": [{"profile": profile, **totals}], "models": models, "providers": providers,
            "days": [], "top_chats": chats, "chat_count": chat_count, "coverage": coverage,
            "coverage_since": since, "partial": True,
            "coverage_reason": "Retained per-chat running totals and named auxiliary counters, not complete provider history. "
                               "Coverage dates are earliest included chat starts, not request timestamps. "
                               "Stored main-loop model/provider attribution may span model switches.",
            "basis": "standard_api_list_price", "currency": "USD"}
