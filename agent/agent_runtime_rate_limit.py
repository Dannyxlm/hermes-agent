"""Rate-limit credential recovery, shared by every pooled provider."""

import time

from agent.credential_pool import (
    EXHAUSTED_TTL_429_SECONDS, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS, STATUS_EXHAUSTED,
    _parse_absolute_timestamp, credential_pool_entry_serves_endpoint,
)

_USAGE_LIMIT_REASON_TOKENS = ("usage_limit_reached", "gousagelimit")
_USAGE_LIMIT_MESSAGE_TOKENS = ("usage limit reached", "usage limit has been reached")


def failed_rate_limit_entry(pool, credential_id, api_key_hint):
    """Match the request key before a stale id, as pool._identify_failed_entry does."""
    entries = pool.entries()
    if api_key_hint:
        return next((entry for entry in entries if entry.runtime_api_key == api_key_hint), None)
    if credential_id:
        return next((entry for entry in entries if entry.id == credential_id), None)
    return pool.current()


def recover_rate_limit(
    agent, pool, *, has_retried_429, error_context, api_key_hint, credential_id,
    rotate_and_swap, rotated_429_ids=None,
):
    """Spend at most one zero-wait lap; retries after that pay the normal retry budget."""
    current = failed_rate_limit_entry(pool, credential_id, api_key_hint)
    rotated = rotated_429_ids if rotated_429_ids is not None else set()
    current_id = getattr(current, "id", None)
    if current_id in rotated:
        return False, True

    def exclude(entry):
        return (
            entry.id == current_id or entry.id in rotated
            or entry.runtime_api_key == (getattr(current, "runtime_api_key", None) or api_key_hint)
            or not credential_pool_entry_serves_endpoint(entry, getattr(agent, "base_url", None))
        )

    available = pool.has_available(model=getattr(agent, "model", None), exclude=exclude)
    if rotated and not available:
        return False, True
    context = error_context or {}
    usage_limit = any(t in str(context.get("reason") or "").lower() for t in _USAGE_LIMIT_REASON_TOKENS) or any(
        t in str(context.get("message") or "").lower() for t in _USAGE_LIMIT_MESSAGE_TOKENS
    )
    pre_exhausted = getattr(current, "last_status", None) == STATUS_EXHAUSTED
    if not available and not (has_retried_429 or usage_limit or pre_exhausted):
        return False, True
    kwargs = {}
    if available:
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
