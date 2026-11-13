"""Read-only Grok subscription billing meter for one saved OAuth grant.

The subscription pool is distinct from API request/token rate limits. No
subscription-allowance response-header contract is established for xAI, so
only the official billing API is used. Never refresh or load a pool here.
"""
from __future__ import annotations

import time

from agent import account_usage as usage
from agent.account_router.types import AccountMeter, MeterWindow
from hermes_cli.auth_constants import _decode_jwt_claims

_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
_USER_URL = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"


def _headers(token: str) -> dict[str, str]:
    # Unverified JWT subject is a routing hint; the service authenticates it.
    subject = _decode_jwt_claims(token).get("sub")
    if (not isinstance(subject, str) or not 0 < len(subject) <= 1024
            or any(not 33 <= ord(char) <= 126 for char in subject)):
        raise ValueError("No usable saved Grok identity")
    return {
        "Authorization": f"Bearer {token}", "X-XAI-Token-Auth": "xai-grok-cli",
        "x-userid": subject, "x-grok-client-version": "1.0.45", "x-grok-client-mode": "headless",
        "Accept": "application/json", "Content-Type": "application/json",
    }


def _reset_epoch(value) -> float | None:
    if not isinstance(value, str) and not usage._is_finite_num(value):
        return None
    try:
        parsed = usage._parse_dt(value)
        epoch = parsed.timestamp() if parsed is not None else None
    except (ValueError, OverflowError, OSError):
        return None
    return epoch if epoch is not None and epoch >= 0 else None


def _window(payload: dict) -> MeterWindow:
    config = payload.get("config") or {}
    period = config.get("currentPeriod") or {}
    reset = _reset_epoch(period.get("end"))
    # Proto3 omits zero-valued fields. Only a VALID period end permits the
    # omitted-percent => zero interpretation; explicit null stays unknown.
    used = config.get("creditUsagePercent", 0 if reset is not None else None)
    if not usage._is_finite_num(used) or not 0 <= used <= 100:
        used = None
    return MeterWindow(
        kind="weekly" if period.get("type") == "USAGE_PERIOD_TYPE_WEEKLY" else "period",
        used_pct=used, resets_at=reset,
    )


def fetch(entry) -> AccountMeter:
    """Two read-only GETs with 5 s timeouts, using only the saved row token."""
    if entry.base_url and entry.base_url.rstrip("/") != "https://api.x.ai/v1":
        raise ValueError("Unsupported usage route")
    token = entry.access_token
    if not token:
        raise ValueError("No usable saved token")
    headers = _headers(token)
    payload = usage._get_json(_BILLING_URL, headers, timeout=5.0)
    profile = usage._get_json(_USER_URL, headers, timeout=5.0)
    plan = profile.get("subscriptionTier")
    return AccountMeter(
        provider="xai-oauth", entry_id=entry.id, plan=plan if isinstance(plan, str) else None,
        windows=(_window(payload),), fetched_at=time.time(), source="usage_api",
    )
