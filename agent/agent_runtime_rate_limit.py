"""Rate-limit credential recovery, shared by every pooled provider."""

from agent.credential_pool import STATUS_EXHAUSTED, credential_pool_entry_serves_endpoint

_USAGE_LIMIT_REASON_TOKENS = ("usage_limit_reached", "gousagelimit")
_USAGE_LIMIT_MESSAGE_TOKENS = ("usage limit reached", "usage limit has been reached")


def _recover_rate_limit(agent, pool, *, has_retried_429, error_context, api_key_hint, credential_id, rotate_and_swap):
    from run_agent import logger

    # Already-exhausted credential: rotate immediately. Avoids the "cancel-between-429s" trap where
    # the local has_retried_429 resets per prompt and retries forever.
    current_entry = None
    if credential_id:
        current_entry = next((e for e in pool.entries() if e.id == credential_id), None)
    if api_key_hint:
        current_entry = current_entry or next(
            (e for e in pool.entries() if e.runtime_api_key == api_key_hint), None
        )
    if current_entry is None:
        current_entry = pool.current()
    current_last_status = getattr(current_entry, "last_status", None) if current_entry else None
    if current_last_status == STATUS_EXHAUSTED:
        logger.info(
            "Credential already exhausted (last_status=%s) — rotating immediately instead of retrying",
            current_last_status,
        )
        return (True, False) if rotate_and_swap(429, "rate limit, pre-exhausted") else (False, True)
    usage_limit_reached = False
    if error_context:
        context_reason = str(error_context.get("reason") or "").lower()
        context_message = str(error_context.get("message") or "").lower()
        usage_limit_reached = any(t in context_reason for t in _USAGE_LIMIT_REASON_TOKENS) or any(
            t in context_message for t in _USAGE_LIMIT_MESSAGE_TOKENS
        )
    if not has_retried_429 and not usage_limit_reached:
        # Reuse pool eligibility (including model cooldowns), but do not count the failed
        # account or another row carrying the same key as an alternate.
        def exclude(entry):
            return (
                entry.id == getattr(current_entry, "id", None)
                or entry.runtime_api_key == (getattr(current_entry, "runtime_api_key", None) or api_key_hint)
                or not credential_pool_entry_serves_endpoint(entry, getattr(agent, "base_url", None))
            )

        if not pool.has_available(model=getattr(agent, "model", None), exclude=exclude):
            return False, True
    return (True, False) if rotate_and_swap(429, "rate limit") else (False, True)
