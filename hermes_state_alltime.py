"""SQL rollups of retained per-session counters, without request-history inference."""
import json
from functools import lru_cache
from math import isclose

from agent.api_value_pricing import estimate_api_value
from agent.usage_pricing import CanonicalUsage
from hermes_state_rollup import _COUNTERS, _SUMS, _metrics, follows_usage_parent

_LINEAGE = """WITH RECURSIVE routes AS MATERIALIZED (
    SELECT s.id, CASE WHEN p.id IS NOT NULL
        AND usage_follows_parent(s.source, s.model_config, p.end_reason)
        THEN p.id END AS parent_id
    FROM sessions s LEFT JOIN sessions p ON p.id=s.parent_session_id
), lineage(origin, id) AS (
    SELECT id, id FROM routes
    UNION
    SELECT l.origin, r.parent_id FROM lineage l JOIN routes r ON r.id=l.id
    WHERE r.parent_id IS NOT NULL
), roots AS (
    SELECT l.origin, COALESCE(MIN(CASE WHEN r.parent_id IS NULL THEN l.id END), MIN(l.id)) AS chat_id
    FROM lineage l JOIN routes r ON r.id=l.id GROUP BY l.origin
), counters AS (
    SELECT r.chat_id, s.started_at, COALESCE(s.model, '') AS model,
        COALESCE(s.billing_provider, '') AS provider,
        {counters}
    FROM sessions s JOIN roots r ON r.origin=s.id
    {where}
), priced AS MATERIALIZED (
    SELECT *, session_api_value(model, provider, {arguments}) AS price FROM counters
), usage AS (
    SELECT *, json_extract(price, '$.estimated_cost_usd') AS estimated_cost_usd,
        json_extract(price, '$.reason') AS price_reason,
        json_extract(price, '$.pricing_date') AS pricing_date,
        json_extract(price, '$.source_url') AS source_url FROM priced
) """
_AGGREGATES = (_SUMS + ", NULL AS turn_count, NULL AS event_count, NULL AS unattributed_turn_events, "
               "COALESCE(SUM(e.estimated_cost_usd),0) AS known_estimated_cost_usd, "
               "SUM(e.estimated_cost_usd IS NULL) AS unknown_price_events, "
               "GROUP_CONCAT(DISTINCT e.price_reason) AS cost_reason, "
               "GROUP_CONCAT(DISTINCT e.pricing_date) AS pricing_dates, "
               "GROUP_CONCAT(DISTINCT e.source_url) AS price_sources")
_TOTAL_TOKENS = "+".join(f"e.{key}" for key in _COUNTERS[:4])


@lru_cache(maxsize=512)
def _unit_rates(model, provider):
    return tuple(estimate_api_value(model, CanonicalUsage(**{key: 1}), provider=provider)
                 ["estimated_cost_usd"] or 0 for key in _COUNTERS[:4])


def _session_value(model, provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, api_call_count):
    usage = CanonicalUsage(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens)
    value = estimate_api_value(model, usage, provider=provider)
    if value["estimated_cost_usd"] is not None:
        if not usage.total_tokens and api_call_count:
            value.update(estimated_cost_usd=None, reason="token usage unavailable for completed call")
        elif api_call_count != 1:
            # A cumulative prompt crossing a request tier does not prove any request crossed it.
            unit_cost = sum(rate * getattr(usage, key) for key, rate in zip(_COUNTERS[:4], _unit_rates(model, provider)))
            if not isclose(value["estimated_cost_usd"], unit_cost, rel_tol=1e-12, abs_tol=1e-12):
                value.update(estimated_cost_usd=None,
                             reason="per-chat totals: original call boundaries unavailable for context-tier pricing")
    return json.dumps(value)


def _session_metrics(row):
    result = _metrics(row)
    result.update(turn_count=None, event_count=None, unattributed_turn_events=None)
    return result


def read_alltime_usage(conn, *, end, profile, session_id=None, limit=20, timezone_name="UTC"):
    """All retained sessions, including zero counters and post-collector work, in one read snapshot."""
    cached_value = lru_cache(maxsize=8192)(_session_value)
    conn.create_function("session_api_value", 7, cached_value, deterministic=True)
    conn.create_function("usage_follows_parent", 3, follows_usage_parent, deterministic=True)
    cte = _LINEAGE.format(
        counters=", ".join(f"COALESCE(s.{key},0) AS {key}" for key in _COUNTERS),
        arguments=", ".join(_COUNTERS[:4]) + ", api_call_count",
        where="WHERE r.chat_id=(SELECT chat_id FROM roots WHERE origin=?)" if session_id else "")
    args = (session_id,) if session_id else ()
    base = cte + "SELECT {columns}" + _AGGREGATES + " FROM usage e {suffix}"
    totals_row = conn.execute(base.format(
        columns="MIN(e.started_at) AS since, COUNT(DISTINCT e.chat_id) AS chat_count, ", suffix=""), args).fetchone()
    since, chat_count = totals_row["since"], totals_row["chat_count"]
    totals = _session_metrics({key: totals_row[key] for key in totals_row.keys() if key not in {"since", "chat_count"}})
    def groups(columns, group, order="total_tokens DESC", extra_args=()):
        return [_session_metrics(row) for row in conn.execute(base.format(
            columns=f"{columns}, SUM({_TOTAL_TOKENS}) AS total_tokens, ",
            suffix=f"GROUP BY {group} ORDER BY {order}"), args + extra_args)]
    models = groups("e.model, e.provider", "e.model, e.provider", "total_tokens DESC, e.model, e.provider")
    providers = groups("e.provider", "e.provider", "total_tokens DESC, e.provider")
    chats = groups("e.chat_id AS session_id, (SELECT title FROM sessions WHERE id=e.chat_id) AS title",
                   "e.chat_id", "total_tokens DESC, e.chat_id LIMIT ?", (limit,))
    coverage = [dict(row) for row in conn.execute(
        cte + "SELECT provider, MIN(started_at) AS since, COUNT(DISTINCT chat_id) AS chat_count, "
        "COUNT(*) AS session_count FROM counters GROUP BY provider ORDER BY provider", args)]
    return {"profile": profile, "window": "all", "source": "session_totals",
            "start": since, "end": end, "timezone": timezone_name, "totals": totals,
            "agents": [{"profile": profile, **totals}], "models": models, "providers": providers,
            "days": [], "top_chats": chats, "chat_count": chat_count, "coverage": coverage,
            "coverage_since": since, "partial": True,
            "coverage_reason": "Retained per-chat running totals, not complete provider history. "
                               "Coverage dates are earliest included chat starts, not request timestamps. "
                               "Stored model/provider attribution may span model switches; auxiliary-only counters are not included.",
            "basis": "standard_api_list_price", "currency": "USD"}
