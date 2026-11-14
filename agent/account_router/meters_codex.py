"""Read-only Codex subscription allowance and response-header windows.

Only fixed official endpoints receive a row's saved bearer. No resolver, pool,
refresh, inference, or reset-credit redemption runs here. Errors propagate;
callers must mark the meter unknown without logging exception bodies.
"""
from __future__ import annotations

import math
import time
from typing import Mapping

from agent import account_usage as usage
from agent.account_router.types import AccountMeter, MeterWindow
from hermes_cli.auth_constants import _decode_jwt_claims

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_ALLOWED_BASES = {"https://chatgpt.com/backend-api", "https://chatgpt.com/backend-api/codex"}


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _epoch(value) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _percent(value) -> float | None:
    # JSON strings are not usage numbers; header strings are decoded separately.
    if not usage._is_finite_num(value) or not 0 <= value <= 100:
        return None
    return float(value)


def _kind(seconds, fallback: str) -> str:
    duration = _number(seconds)
    if duration is None:
        return fallback
    return {18000.0: "session", 604800.0: "weekly"}.get(duration, "period")


def _windows(rate_limit: dict) -> tuple[MeterWindow, ...]:
    windows = []
    for key, label in usage._codex_window_labels(rate_limit):
        window = rate_limit.get(key)
        if not window:
            continue
        windows.append(MeterWindow(
            kind=_kind(window.get("limit_window_seconds"), label.lower()),
            used_pct=_percent(window.get("used_percent")),
            resets_at=_epoch(window.get("reset_at")),
        ))
    return tuple(windows)


def _credits(payload: dict) -> float | None:
    credits = payload.get("credits") or {}
    if credits.get("has_credits") is False:
        return 0.0
    balance = _number(credits.get("balance"))
    return balance if balance is not None and balance >= 0 else None


def fetch(entry) -> AccountMeter:
    """Fetch THIS row's saved grant; a failed request is unknown, never free."""
    if entry.base_url and entry.base_url.rstrip("/") not in _ALLOWED_BASES:
        raise ValueError("Unsupported usage route")
    token = entry.access_token
    if not token:
        raise ValueError("No usable saved token")
    claims = _decode_jwt_claims(token).get("https://api.openai.com/auth") or {}
    claims = claims if isinstance(claims, dict) else {}
    payload = usage._get_json(
        _USAGE_URL, usage._codex_headers(token, claims.get("chatgpt_account_id")), timeout=5.0,
    )
    plan = payload.get("plan_type") or claims.get("chatgpt_plan_type")
    return AccountMeter(
        provider="openai-codex", entry_id=entry.id,
        plan=plan if isinstance(plan, str) else None,
        windows=_windows(payload.get("rate_limit") or {}), fetched_at=time.time(), source="usage_api",
        extras={"banked_resets": usage._codex_banked_resets(payload), "credits": _credits(payload)},
    )


def parse_headers(headers: Mapping[str, str], *, now: float | None = None) -> tuple[MeterWindow, ...]:
    """Parse only account-wide x-codex-* quota, NOT generic RPM/TPM headers.

    Codex's primary slot may contain the weekly window. The published duration
    overrides position; reset-at overrides reset-after-seconds. Model-specific
    limit-id families are deliberately not promoted to whole-account caps.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    captured_at = time.time() if now is None else now
    windows = []
    for position, fallback in (("primary", "session"), ("secondary", "weekly")):
        prefix = f"x-codex-{position}-"
        if prefix + "used-percent" not in lowered:
            continue
        minutes = _number(lowered.get(prefix + "window-minutes"))
        reset = _epoch(lowered.get(prefix + "reset-at"))
        delay = _epoch(lowered.get(prefix + "reset-after-seconds"))
        if reset is None and delay is not None:
            reset = _epoch(captured_at + delay)
        windows.append(MeterWindow(
            kind=_kind(minutes * 60 if minutes is not None else None, fallback),
            used_pct=_percent(_number(lowered.get(prefix + "used-percent"))), resets_at=reset,
        ))
    return tuple(windows)
