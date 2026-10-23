"""Offline API-list-price value, NOT the provider invoice or plan allowance.

Snapshot retrieved 2026-10-19. Exact model ids only; never guess by family.
The detailed developer rate card is authoritative when the marketing page lags.
CanonicalUsage buckets are disjoint; reasoning is already included in output.
"""
from decimal import Decimal

from agent.usage_pricing import CanonicalUsage

PRICING_DATE = "2026-10-19"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
# USD / million: uncached input, output, cache read, cache write (5m for Claude).
_OPENAI = {
    "gpt-6-astra": ("10", "50", "1", "12.5"),
    "gpt-6.1-sol": ("2", "10", "0.1", "2.5"),
    "gpt-6-sol": ("2", "10", "0.2", "2.5"),
    "gpt-6-luna": ("0.1", "0.5", "0.01", "0.125"),
    "gpt-5.6-sol": ("4", "20", "0.4", "5"),
    "gpt-5.6-terra": ("2", "12", "0.2", "2.5"),
    "gpt-5.6-luna": ("0.2", "1.2", "0.02", "0.25"),
    "gpt-5.5": ("5", "30", "0.5", None),
    "gpt-5.4": ("2.5", "15", "0.25", None),
    "gpt-5.4-mini": ("0.75", "4.5", "0.075", None),
    "gpt-5.4-nano": ("0.2", "1.25", "0.02", None),
    "gpt-5.3-codex": ("1.75", "14", "0.175", None),
    "gpt-5.2-codex": ("1.75", "14", "0.175", None),
    "gpt-5.2": ("1.75", "14", "0.175", None),
    "gpt-5.1": ("1.25", "10", "0.125", None),
    "gpt-4.1": ("2", "8", "0.5", None),
    "gpt-4.1-mini": ("0.4", "1.6", "0.1", None),
    "gpt-4o": ("2.5", "10", "1.25", None),
    "gpt-4o-mini": ("0.15", "0.6", "0.075", None),
}
_ANTHROPIC = {
    "claude-opus-5-5": ("4", "20", "0.2", "5"),
    "claude-opus-5": ("5", "25", "0.5", "6.25"),
    "claude-opus-4-8": ("5", "25", "0.5", "6.25"),
    "claude-opus-4-7": ("5", "25", "0.5", "6.25"),
    "claude-opus-4-6": ("5", "25", "0.5", "6.25"),
    "claude-sonnet-5-5": ("2", "10", "0.1", "2.5"),
    "claude-sonnet-5": ("2", "10", "0.2", "2.5"),
    "claude-sonnet-4-6": ("3", "15", "0.3", "3.75"),
    "claude-haiku-4-5": ("1", "5", "0.1", "1.25"),
}
_LONG_CONTEXT = frozenset({"gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
                           "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4"})


def estimate_api_value(model: str, usage: CanonicalUsage, *, provider: str) -> dict:
    """API-list-price wire value. Decimal calculation is shared with aggregate estimates."""
    result = estimate_api_value_decimal(model, usage, provider=provider)
    if result["estimated_cost_usd"] is not None:
        result["estimated_cost_usd"] = float(result["estimated_cost_usd"])
    return result


def estimate_aggregate_api_value_decimal(model: str, usage: CanonicalUsage, *, provider: str,
                                         api_call_count: int,
                                         tier_reason: str = "aggregate usage: original call boundaries unavailable for context-tier pricing") -> dict:
    """Retained counters are exact only when missing boundaries cannot change rates."""
    # Counter-only aggregates cannot identify omitted usage. Exactness assumes every counted call reported tokens.
    value = estimate_api_value_decimal(model, usage, provider=provider)
    minimum = value["estimated_cost_usd"]
    if minimum is not None:
        if not usage.total_tokens and api_call_count:
            value.update(estimated_cost_usd=None, status="unknown", reason="token usage unavailable for completed call")
            minimum = None
        elif api_call_count != 1:
            standard = estimate_api_value_decimal(model, usage, provider=provider, standard_tier=True)
            if minimum != standard["estimated_cost_usd"]:
                value.update(estimated_cost_usd=None, status="minimum", reason=tier_reason)
                minimum = standard["estimated_cost_usd"]
    value["minimum_estimated_cost_usd"] = minimum
    return value


def _api_value_rate_card(model: str, *, provider: str):
    from agent.model_metadata import strip_codex_context_variant_suffix
    if provider == "openai-codex":
        model = strip_codex_context_variant_suffix(model)
    vendor = "openai" if provider in {"openai", "openai-codex"} else provider
    table, source = ({"openai": (_OPENAI, OPENAI_SOURCE), "anthropic": (_ANTHROPIC, ANTHROPIC_SOURCE)}
                     .get(vendor, ({}, None)))
    if model.startswith(vendor + "/"):
        model = model[len(vendor) + 1:]
    return model, table.get(model), source


def get_api_value_rates(model: str, *, provider: str) -> dict | None:
    """Offline base-tier USD list rates per million, independent of usage or billing mode."""
    _, rates, source = _api_value_rate_card(model, provider=provider)
    if rates is None:
        return None
    return {**dict(zip(("input_usd_per_million", "output_usd_per_million",
                        "cache_read_usd_per_million", "cache_write_usd_per_million"),
                       (float(rate) if rate is not None else None for rate in rates))),
            "pricing_date": PRICING_DATE, "source_url": source}


def estimate_api_value_decimal(model: str, usage: CanonicalUsage, *, provider: str,
                               standard_tier: bool = False) -> dict:
    """Standard processing API equivalent. Discounts, speed tiers, tools and tax excluded.

    This deliberately ignores subscription billing_mode. Above 272K prompt tokens,
    OpenAI's long-context rates apply to the whole request, not marginal tokens.
    standard_tier skips that premium to bound cumulative usage with unknown call boundaries.
    """
    model, rates, source = _api_value_rate_card(model, provider=provider)
    result = {"estimated_cost_usd": None, "status": "unknown", "reason": None,
              "pricing_date": PRICING_DATE, "source_url": source,
              "basis": "standard_api_list_price", "currency": "USD"}
    if rates is None:
        return {**result, "reason": "model/provider not in the sourced price table"}
    counts = (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens)
    if any(type(n) is not int or n < 0 for n in counts):
        return {**result, "reason": "invalid token counts"}
    cost = Decimal(0)
    for name, tokens, rate in zip(("input", "output", "cache_read", "cache_write"), counts, rates):
        if tokens and rate is None:
            return {**result, "reason": f"{name} price not published in the snapshot"}
        if rate is not None:
            multiplier = Decimal(1)
            if not standard_tier and model in _LONG_CONTEXT and usage.prompt_tokens > 272000:
                multiplier = Decimal("1.5") if name == "output" else Decimal(2)
            cost += Decimal(tokens) * Decimal(rate) * multiplier / Decimal(1000000)
    return {**result, "estimated_cost_usd": cost, "status": "est"}
