"""Timestamped usage projection. Writes join the existing accounting transaction;
reads use indexed metadata only, never messages or JSONL transcripts.
"""
import json
import time
from decimal import Decimal
from functools import lru_cache
from math import nextafter

from agent.api_value_pricing import estimate_aggregate_api_value_decimal
from agent.usage_pricing import CanonicalUsage

_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "api_call_count")
_SUMS = ", ".join(f"COALESCE(SUM(e.{key}),0) AS {key}" for key in _COUNTERS)
_AGGREGATES = (_SUMS + ", COUNT(DISTINCT e.turn_id) AS turn_count, COUNT(*) AS event_count, "
               "SUM(e.turn_id IS NULL AND e.task='') AS unattributed_turn_events, "
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
            self.total += Decimal(value)

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
                 api_call_count, exact, pricing_date, source_url):
    if exact is not None:
        value = {"estimated_cost_usd": str(exact), "minimum_estimated_cost_usd": str(exact),
                 "reason": None, "pricing_date": pricing_date, "source_url": source_url}
    else:
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

    Missing historical timestamps are NOT invented from session first/last_seen.
    Coverage starts when the incremental collector was installed.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(timezone_name)
    conn.create_function("usage_day", 1, lambda stamp: datetime.fromtimestamp(stamp, zone).date().isoformat(),
                         deterministic=True)
    conn.create_function("event_api_value", 10, lru_cache(maxsize=8192)(_event_value), deterministic=True)
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    where = "e.occurred_at>=? AND e.occurred_at<?"
    args = [start, end]
    if session_id:
        where += " AND e.chat_id=?"
        args.append(_chat_id(conn, session_id))
    arguments = ", ".join(_COUNTERS[:4]) + ", api_call_count, estimated_cost_usd, pricing_date, source_url"
    cte = (f"WITH priced AS MATERIALIZED (SELECT *, event_api_value(model, provider, {arguments}) AS price "
           f"FROM usage_events e WHERE {where}), usage AS (SELECT "
           "chat_id, occurred_at, turn_id, model, provider, task, " + ", ".join(_COUNTERS) + ", "
           "json_extract(price, '$.estimated_cost_usd') AS estimated_cost_usd, "
           "json_extract(price, '$.minimum_estimated_cost_usd') AS minimum_estimated_cost_usd, "
           "json_extract(price, '$.reason') AS price_reason, "
           "json_extract(price, '$.pricing_date') AS pricing_date, "
           "json_extract(price, '$.source_url') AS source_url FROM priced) ")
    base = "FROM usage e"
    totals = _metrics(conn.execute(f"{cte}SELECT {_AGGREGATES} {base}", args).fetchone())
    def groups(columns, group, order="total_tokens DESC"):
        rows = conn.execute(f"{cte}SELECT {columns}, {_AGGREGATES}, "
                            "SUM(e.input_tokens+e.output_tokens+e.cache_read_tokens+e.cache_write_tokens) AS total_tokens "
                            f"{base} GROUP BY {group} ORDER BY {order}", args).fetchall()
        return [_metrics(row) for row in rows]
    chats = groups("e.chat_id AS session_id", "e.chat_id", "total_tokens DESC, e.chat_id")
    for chat in chats[:limit]:
        row = conn.execute("SELECT title FROM sessions WHERE id=?", (chat["session_id"],)).fetchone()
        chat["title"] = row[0] if row else None
    since = conn.execute("SELECT value FROM state_meta WHERE key='usage_events_since'").fetchone()
    return {"profile": profile, "start": start, "end": end, "timezone": timezone_name, "totals": totals,
            "agents": [{"profile": profile, **totals}],
            "models": groups("e.model, e.provider", "e.model, e.provider"),
            "providers": groups("e.provider", "e.provider"),
            "days": groups("usage_day(e.occurred_at) AS day, e.provider", "day, e.provider", "day, e.provider"),
            "top_chats": chats[:limit], "chat_count": len(chats),
            "coverage_since": float(since[0]) if since else None,
            "partial": not since or start < float(since[0]),
            "coverage_reason": "Timestamped usage is collected prospectively; legacy cumulative counters cannot be split into windows.",
            "basis": "standard_api_list_price", "currency": "USD"}
