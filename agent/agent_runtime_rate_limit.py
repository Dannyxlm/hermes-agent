"""Rate-limit credential recovery, shared by every pooled provider."""

import math
import time

from agent.credential_pool import (
    EXHAUSTED_TTL_429_SECONDS, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS, STATUS_EXHAUSTED,
    _parse_absolute_timestamp, credential_pool_entry_serves_endpoint,
)

_USAGE_LIMIT_REASON_TOKENS = ("usage_limit_reached", "gousagelimit")
_USAGE_LIMIT_MESSAGE_TOKENS = ("usage limit reached", "usage limit has been reached")


def _retry_once_wait_exceeded(context):
    """Opt out of retry-once only for a known reset beyond the hot-reloaded policy."""
    from agent.fallback_cooldown import _provider_reset_delay
    from hermes_cli.config import load_config_readonly

    try:
        threshold = float(load_config_readonly().get("fallback", {}).get("retry_once_max_wait_seconds") or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    if not math.isfinite(threshold) or threshold <= 0:
        return False
    # Extraction already applies body-reset / Retry-After precedence. bench_until can carry
    # a longer quota window for rotation; it must not turn a near retry into a far one.
    delay = _provider_reset_delay(context.get("reset_at"))
    return delay is not None and delay > threshold


def recover_rate_limit(
    agent, pool, *, has_retried_429, error_context, api_key_hint, credential_id,
    rotate_and_swap, rotated_429_ids=None,
):
    """Spend at most one zero-wait lap; retries after that pay the normal retry budget."""
    current = pool.identify_failed_entry(credential_id, api_key_hint, allow_stale_key=True)
    rotated = rotated_429_ids if rotated_429_ids is not None else set()
    current_id = getattr(current, "id", None)
    def exclude(entry):
        return (
            entry.id == current_id or entry.id in rotated
            or entry.runtime_api_key == (getattr(current, "runtime_api_key", None) or api_key_hint)
            or not credential_pool_entry_serves_endpoint(entry, getattr(agent, "base_url", None))
        )

    available = pool.has_available(model=getattr(agent, "model", None), exclude=exclude)
    free = available and current_id not in rotated
    context = error_context or {}
    usage_limit = any(t in str(context.get("reason") or "").lower() for t in _USAGE_LIMIT_REASON_TOKENS) or any(
        t in str(context.get("message") or "").lower() for t in _USAGE_LIMIT_MESSAGE_TOKENS
    )
    pre_exhausted = getattr(current, "last_status", None) == STATUS_EXHAUSTED
    if not free and not (has_retried_429 or usage_limit or pre_exhausted or _retry_once_wait_exceeded(context)):
        return False, True
    kwargs = {}
    if free:
        # Short hints must not let a just-failed account immediately rejoin a free lap.
        now = time.time()
        hint = max(_parse_absolute_timestamp(context.get("bench_until")) or 0,
                   _parse_absolute_timestamp(context.get("reset_at")) or 0)
        hint = hint if hint > now else 0
        kwargs["bench_until"] = max(
            now + (EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS if hint else EXHAUSTED_TTL_429_SECONDS), hint,
        )
    recovered = rotate_and_swap(429, "rate limit", **kwargs)
    if recovered and current_id is not None:
        rotated.add(current_id)
    return (True, False) if recovered else (False, True)
