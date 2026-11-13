"""Pure use-it-or-lose-it scoring. Unknown is never confused with exhausted."""
from __future__ import annotations

import math

from .types import AccountMeter

DEFAULT_POLICY = {
    "pin_hours": 6, "min_dwell_minutes": 120, "switch_margin": 0.25,
    "cap_pct": 98, "max_meter_age_minutes": 30,
    "plan_units": {"anthropic": {"default_claude_ai": 1, "default_claude_max_5x": 5,
                                  "default_claude_max_20x": 20},
                   # Relative allowance heuristics, not verified weekly token quotas.
                   "openai-codex": {"plus": 1, "prolite": 10, "pro": 20},
                   # Legacy billing tier labels do not establish a relative quota ratio.
                   "xai-oauth": {"GrokPro": 1, "XPremiumPlus": 1}},
}


def scope_key(model):
    model = str(model or "").lower()
    return next((key for key in ("fable", "opus", "sonnet")
                 if model.startswith("claude-" + key + "-")), None)


def window_key(window):
    return window.kind + "/" + (window.model_scope or "all")


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _score(provider, scope, meter, now, policy) -> tuple[float | None, str]:
    age = policy.get("max_meter_age_minutes", 30) * 60
    if not isinstance(meter, AccountMeter) or not _finite(meter.fetched_at) or now - meter.fetched_at > age:
        return None, "unknown or stale meter"
    windows = [w for w in meter.windows if w.model_scope is None or w.model_scope == scope]
    stamps = meter.extras.get("window_fetched_at", {})
    fresh = [w for w in windows if now - stamps.get(window_key(w), meter.fetched_at) <= age]
    if any(_finite(w.used_pct) and w.used_pct >= policy.get("cap_pct", 98) for w in fresh):
        return 0.0, "applicable allowance capped"
    if len(fresh) != len(windows):
        return None, "stale window"
    periods = [w for w in windows if w.kind in ("weekly", "period")]
    if not periods or any(not _finite(w.used_pct) or not 0 <= w.used_pct <= 100
                          or not _finite(w.resets_at) or w.resets_at <= now for w in periods):
        return None, "unknown allowance or reset"
    binding = max(periods, key=lambda w: w.used_pct or 0)
    plans = dict(DEFAULT_POLICY["plan_units"].get(provider, {}))
    plans.update(policy.get("plan_units", {}).get(provider, {}))
    units = plans.get(meter.plan, 1)
    if not _finite(units) or units <= 0:
        units = 1
    score = (100 - binding.used_pct) / 100 * units / max((binding.resets_at - now) / 3600, 1)
    return score, "remaining plan units / hours to reset"


def rank(provider, model, entry_ids_in_static_order, meters, now, policy) -> list[tuple[str, float | None, str]]:
    scope = scope_key(model)
    rows = [(id, *_score(provider, scope, meters.get(id), now, policy))
            for id in entry_ids_in_static_order]
    if all(score is None for _, score, _ in rows):
        return rows
    return sorted(rows, key=lambda row: (0, -row[1]) if row[1] is not None and row[1] > 0
                  else (1, 0) if row[1] is None else (2, 0))
