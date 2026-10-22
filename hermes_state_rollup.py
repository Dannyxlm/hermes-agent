"""Timestamped usage projection. Writes join the existing accounting transaction;
reads use indexed metadata only, never messages or JSONL transcripts.
"""
import json
import time
from decimal import Decimal
from math import nextafter

from agent.api_value_pricing import estimate_aggregate_api_value_decimal, get_api_value_rates
from agent.usage_pricing import CanonicalUsage

_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "api_call_count")
_SUMS = ", ".join(f"COALESCE(SUM(e.{key}),0) AS {key}" for key in _COUNTERS)
_AGGREGATES = (_SUMS + ", COUNT(DISTINCT e.turn_id) AS turn_count, COALESCE(SUM(e.is_event),0) AS event_count, "
               "SUM(e.is_event AND e.turn_id IS NULL AND e.task='') AS unattributed_turn_events, "
               "decimal_sum(e.estimated_cost_usd) AS known_estimated_cost_usd, "
               "decimal_sum(e.minimum_estimated_cost_usd) AS minimum_estimated_cost_usd, "
               "SUM(e.estimated_cost_usd IS NULL) AS unknown_price_events, "
               "SUM(e.estimated_cost_usd IS NULL AND e.minimum_estimated_cost_usd IS NOT NULL) AS minimum_price_events, "
               "GROUP_CONCAT(DISTINCT e.price_reason) AS cost_reason, "
               "GROUP_CONCAT(DISTINCT e.pricing_date) AS pricing_dates, "
               "GROUP_CONCAT(DISTINCT e.source_url) AS price_sources")


class _DecimalSum:
    def __init__(self):
        self.total = Decimal(0)

    def step(self, value):
        if value is not None:
            self.total += Decimal(str(value)) if isinstance(value, float) else Decimal(value)

    def finalize(self):
        return str(self.total)


def _minimum_float(value):
    amount = float(value)
    # JSON numbers must not round an exact Decimal lower bound upward.
    if Decimal(str(amount)) > value:
        amount = nextafter(amount, 0.0)
    return amount


def follows_usage_parent(source, model_config, parent_end_reason):
    delegated = source in {"subagent", "delegate"}
    if model_config:
        try:
            config = json.loads(model_config)
            if isinstance(config, dict) and config.get("_branched_from"):
                return False
            delegated = delegated or isinstance(config, dict) and bool(config.get("_delegate_from"))
        except (ValueError, TypeError):
            pass
    return parent_end_reason == "compression" or delegated


def _chat_id(conn, session_id):
    """Compression and delegated work belong to the owning chat; explicit branches do not."""
    seen = set()
    while session_id not in seen:
        seen.add(session_id)
        row = conn.execute("SELECT s.parent_session_id, s.source, s.model_config, p.end_reason "
                           "FROM sessions s LEFT JOIN sessions p ON p.id=s.parent_session_id WHERE s.id=?",
                           (session_id,)).fetchone()
        if not row or not row[0]:
            break
        if not follows_usage_parent(row[1], row[2], row[3]):
            break
        session_id = row[0]
    return session_id


def record_usage_event(conn, session_id, *, model, provider, counts, task="", turn_id=None, occurred_at=None):
    usage = CanonicalUsage(**{key: int(counts.get(key) or 0) for key in _COUNTERS if key != "api_call_count"})
    value = estimate_aggregate_api_value_decimal(model, usage, provider=provider,
                                                api_call_count=int(counts.get("api_call_count") or 0))
    exact = value["estimated_cost_usd"]
    conn.execute("""INSERT INTO usage_events (
        session_id, chat_id, occurred_at, turn_id, model, provider, task,
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, api_call_count,
        estimated_cost_usd, price_reason, pricing_date, source_url)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (session_id, _chat_id(conn, session_id), time.time() if occurred_at is None else occurred_at,
         turn_id or None, model, provider, task, *[int(counts.get(k) or 0) for k in _COUNTERS],
         float(exact) if exact is not None else None, value["reason"], value["pricing_date"], value["source_url"]))


def _event_value(model, provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                 api_call_count):
    usage = CanonicalUsage(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens)
    value = estimate_aggregate_api_value_decimal(model, usage, provider=provider, api_call_count=api_call_count)
    return json.dumps(value, default=str)


def _metrics(row):
    result = dict(row)
    minimum_events = int(result.pop("minimum_price_events") or 0)
    for key in ("unknown_price_events", "unattributed_turn_events"):
        result[key] = int(result[key] or 0)
    result["known_estimated_cost_usd"] = float(result["known_estimated_cost_usd"] or 0)
    result["minimum_estimated_cost_usd"] = _minimum_float(Decimal(result["minimum_estimated_cost_usd"] or 0))
    for key in ("pricing_dates", "price_sources"):
        result[key] = sorted((result[key] or "").split(",")) if result[key] else []
    result["total_tokens"] = sum(result[k] for k in _COUNTERS[:4])
    prompt = result["input_tokens"] + result["cache_read_tokens"] + result["cache_write_tokens"]
    result["cache_hit_percent"] = 100 * result["cache_read_tokens"] / prompt if prompt else None
    result["estimated_cost_usd"] = None if result["unknown_price_events"] else result["known_estimated_cost_usd"]
    result["cost_status"] = ("unknown" if result["unknown_price_events"] > minimum_events
                             else "minimum" if minimum_events else "est")
    return result


def read_usage_rollup(conn, *, start, end, profile, session_id=None, limit=20, timezone_name="UTC"):
    """One read snapshot; totals are never limited by the top-chat page.

    Pre-collector history is a session-start cohort, not reconstructed event timestamps.
    Sessions starting before the window are excluded from that cohort.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(timezone_name)
    conn.create_function("usage_day", 1, lambda stamp: datetime.fromtimestamp(stamp, zone).date().isoformat(),
                         deterministic=True)
    conn.create_function("event_api_value", 7, _event_value, deterministic=True)
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    since = conn.execute("SELECT value FROM state_meta WHERE key='usage_events_since'").fetchone()
    coverage_since = float(since[0]) if since else None
    backfilled = coverage_since is not None and start < coverage_since <= end
    where = "e.occurred_at>=:start AND e.occurred_at<:end"
    args = {"start": start, "end": end, "coverage_since": coverage_since, "session_id": session_id}
    if session_id:
        where += " AND e.chat_id=:chat_id"
        args["chat_id"] = _chat_id(conn, session_id)
    prefix = "WITH "
    counters = ", ".join(_COUNTERS)
    raw = ("SELECT session_id, chat_id, occurred_at, turn_id, model, provider, task, " + counters + ", "
           "estimated_cost_usd, pricing_date, source_url, 1 AS is_event FROM window_events")
    if backfilled:
        from hermes_state_backfill import backfill_usage_cte
        conn.create_function("usage_follows_parent", 3, follows_usage_parent, deterministic=True)
        prefix = backfill_usage_cte(session_id=session_id) + ", "
        raw += (" UNION ALL SELECT session_id, chat_id, started_at, NULL, model, provider, task, "
                + counters + ", NULL, NULL, NULL, 0 FROM backfill")
    arguments = ", ".join(_COUNTERS[:4]) + ", api_call_count"
    cte = (prefix + f"window_events AS MATERIALIZED (SELECT * FROM usage_events e WHERE {where}), "
           f"raw_usage AS NOT MATERIALIZED ({raw}), priced AS MATERIALIZED (SELECT *, CASE WHEN estimated_cost_usd IS NULL "
           f"THEN event_api_value(model, provider, {arguments}) END AS price "
           "FROM raw_usage), usage AS MATERIALIZED (SELECT "
           "session_id, chat_id, occurred_at, turn_id, model, provider, task, is_event, " + counters + ", "
           "CASE WHEN price IS NULL THEN estimated_cost_usd ELSE "
           "json_extract(price, '$.estimated_cost_usd') END AS estimated_cost_usd, "
           "CASE WHEN price IS NULL THEN estimated_cost_usd ELSE "
           "json_extract(price, '$.minimum_estimated_cost_usd') END AS minimum_estimated_cost_usd, "
           "CASE WHEN price IS NULL THEN NULL ELSE json_extract(price, '$.reason') END AS price_reason, "
           "CASE WHEN price IS NULL THEN pricing_date ELSE "
           "json_extract(price, '$.pricing_date') END AS pricing_date, "
           "CASE WHEN price IS NULL THEN source_url ELSE "
           "json_extract(price, '$.source_url') END AS source_url FROM priced) ")
    projections = (
        ("totals", "json_object('backfill_session_count', COUNT(DISTINCT CASE WHEN e.is_event=0 THEN e.session_id END))", ""),
        ("top_chats", "json_object('session_id', e.chat_id)",
         "GROUP BY e.chat_id ORDER BY total_tokens DESC, e.chat_id"),
        ("models", "json_object('model', e.model, 'provider', e.provider)",
         "GROUP BY e.model, e.provider ORDER BY total_tokens DESC"),
        ("providers", "json_object('provider', e.provider)",
         "GROUP BY e.provider ORDER BY total_tokens DESC"),
        ("days", "json_object('day', usage_day(e.occurred_at), 'provider', e.provider)",
         "GROUP BY usage_day(e.occurred_at), e.provider ORDER BY usage_day(e.occurred_at), e.provider"),
    )
    # One statement shares materialization across every grouping, even above the old cache limit.
    queries = [f"SELECT * FROM (SELECT '{kind}' AS kind, {dimensions} AS dimensions, {_AGGREGATES}, "
               "SUM(e.input_tokens+e.output_tokens+e.cache_read_tokens+e.cache_write_tokens) AS total_tokens "
               f"FROM usage e {suffix})" for kind, dimensions, suffix in projections]
    grouped = {kind: [] for kind, _, _ in projections}
    for row in conn.execute(cte + " UNION ALL ".join(queries), args):
        metric = dict(row)
        kind = metric.pop("kind")
        dimensions = json.loads(metric.pop("dimensions"))
        grouped[kind].append({**dimensions, **_metrics(metric)})
    for model in grouped["models"]:
        model["rates"] = get_api_value_rates(model["model"], provider=model["provider"])
    totals = grouped["totals"][0]
    session_count = totals.pop("backfill_session_count")
    backfill = ({"since": start, "until": coverage_since, "basis": "session_totals_by_start",
                 "session_count": session_count} if backfilled else None)
    chats = grouped["top_chats"]
    for chat in chats[:limit]:
        row = conn.execute("SELECT title FROM sessions WHERE id=?", (chat["session_id"],)).fetchone()
        chat["title"] = row[0] if row else None
    return {"profile": profile, "start": start, "end": end, "timezone": timezone_name, "totals": totals,
            "agents": [{"profile": profile, **totals}],
            "models": grouped["models"], "providers": grouped["providers"], "days": grouped["days"],
            "top_chats": chats[:limit], "chat_count": len(chats),
            "coverage_since": coverage_since, "backfill": backfill,
            "partial": not backfilled and (coverage_since is None or start < coverage_since),
            "coverage_reason": ("backfilled_from_session_totals" if backfilled else
                                "Timestamped usage is collected prospectively; legacy cumulative counters cannot be split into windows."),
            "basis": "standard_api_list_price", "currency": "USD"}
