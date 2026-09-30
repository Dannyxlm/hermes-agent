"""Typed routing events: what model / provider / account is *actually* serving a session.

Hermes already logs fallback activation, primary restore and credential rotation, and emits
prose lifecycle notices for two of them. Clients (Hermex web, the native iPhone app) had to
regex those sentences, and had no way to learn about a same-model account switch or the
effective reasoning level after a fallback. This module publishes small, credential-free,
typed events on the existing ``status_callback`` rail under ``kind="routing"`` with a JSON
string payload. Nothing here is printed to the CLI; the human-readable notices stay as they were.

Event ``type`` values:

- ``provider_switched`` — cross-provider/model fallback activated.
- ``primary_restored`` — the primary model/provider is serving again (confirmed, not a timer).
- ``account_switched`` — same provider, a different pooled account is now on the wire.
- ``serving`` — snapshot of the route that just served a successful API call (deduped).

Payload fields are limited to model ids, provider ids, account *labels*, reason enums,
booleans, integers and the effective reasoning description. Never tokens, emails or raw
provider bodies.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("run_agent")

ROUTING_STATUS_KIND = "routing"
SCHEMA_VERSION = 1

_REASON_TEXT = {
    "rate_limit": "usage limit",
    "billing": "credits required",
    "auth": "sign-in required",
    "overload": "temporarily overloaded",
    "server_error": "provider error",
    "timeout": "timed out",
    "model_not_found": "model unavailable",
}


def reason_slug(reason: Any) -> Optional[str]:
    """Stable enum-ish slug for a ``FailoverReason`` (or string); None when unknown."""
    if reason is None:
        return None
    value = getattr(reason, "value", None) or getattr(reason, "name", None) or reason
    text = str(value or "").strip().lower()
    return text or None


def reason_text(reason: Any) -> str:
    slug = reason_slug(reason)
    return _REASON_TEXT.get(slug or "", slug or "unavailable")


def account_label(agent: Any, entry_id: Optional[str] = None) -> Optional[str]:
    """Label of the pool entry serving *agent* (or *entry_id*); never the secret."""
    pool = getattr(agent, "_credential_pool", None)
    if pool is None:
        return None
    try:
        cid = entry_id if entry_id is not None else getattr(agent, "_credential_pool_entry_id", None)
        if not cid:
            return None
        label_for_id = getattr(pool, "label_for_id", None)
        if callable(label_for_id):
            label = label_for_id(cid)
            return str(label) if label else None
        entry = next((e for e in pool.entries() if e.id == cid), None)
        return (entry.label or entry.id[:8]) if entry is not None else None
    except Exception:
        return None


def effective_reasoning(agent: Any) -> Dict[str, Any]:
    """Best-effort description of the reasoning level actually on the wire for the active route.

    ``state``: ``explicit`` (an effort goes on the wire), ``disabled`` (a disable goes on the
    wire), ``budget`` (Anthropic budget_tokens), ``default`` (nothing sent; route default applies),
    ``unsupported`` (route accepts no reasoning field), ``unknown`` (could not be resolved).
    ``requested`` is the configured effort before transport normalisation; ``effort`` is what
    the transport resolved. ``source`` says which resolver produced the answer.
    """
    cfg = getattr(agent, "reasoning_config", None)
    cfg = cfg if isinstance(cfg, dict) else {}
    requested = cfg.get("effort")
    requested = str(requested).lower() if requested else None
    enabled = cfg.get("enabled") is not False
    out: Dict[str, Any] = {"requested": requested, "enabled": enabled, "effort": None, "state": "unknown", "source": "configured"}
    model = str(getattr(agent, "model", "") or "")
    provider = str(getattr(agent, "provider", "") or "")
    api_mode = str(getattr(agent, "api_mode", "") or "")
    base_url = getattr(agent, "base_url", None)
    try:
        if api_mode == "anthropic_messages":
            from agent.anthropic_adapter import _thinking_kwargs
            kw = _thinking_kwargs(cfg, model, int(getattr(agent, "max_tokens", None) or 8192))
            thinking = kw.get("thinking") or {}
            out["source"] = "anthropic_adapter"
            if thinking.get("type") == "disabled":
                out.update(effort="none", state="disabled")
            elif thinking.get("type") == "adaptive":
                out.update(effort=(kw.get("output_config") or {}).get("effort"), state="explicit")
            elif thinking.get("type") == "enabled":
                out.update(effort=requested or "medium", state="budget",
                           budget_tokens=thinking.get("budget_tokens"))
            elif not enabled:
                out.update(effort=None, state="default")  # disable not accepted; route default stays on
            else:
                out.update(effort=None, state="unsupported")
            return out
        if provider in ("openai-codex", "xai-oauth") or api_mode == "codex_responses":
            from agent.transports.codex import _resolve_reasoning
            effort, on = _resolve_reasoning(model, {
                "reasoning_config": cfg, "base_url": base_url, "provider": provider,
                "is_codex_backend": provider == "openai-codex", "is_xai_responses": provider == "xai-oauth",
            })
            out["source"] = "codex_transport"
            if effort is None and not on:
                out.update(effort=None, state="unsupported" if enabled else "default")
            elif not on:
                out.update(effort=str(effort), state="disabled")
            else:
                out.update(effort=str(effort), state="explicit")
            return out
    except Exception:
        logger.debug("effective_reasoning resolution failed", exc_info=True)
    if not enabled:
        out.update(effort="none", state="disabled")
    elif requested:
        out.update(effort=requested, state="explicit")
    else:
        out.update(state="default")
    return out


def serving_snapshot(agent: Any) -> Dict[str, Any]:
    """Credential-free description of the route currently bound to *agent*."""
    route = getattr(agent, "_provider_fallback_route", None)
    primary = getattr(agent, "_primary_runtime", None)
    primary = primary if isinstance(primary, dict) else {}
    return {
        "model": str(getattr(agent, "model", "") or ""),
        "provider": str(getattr(agent, "provider", "") or ""),
        "account": account_label(agent),
        "fallback_active": bool(getattr(agent, "_provider_fallback_active", False)),
        "primary_model": str(primary.get("model") or "") or None,
        "primary_provider": str(primary.get("provider") or "") or None,
        "fallback_route": list(route) if isinstance(route, (list, tuple)) else None,
        "reasoning": effective_reasoning(agent),
    }


def _emit(agent: Any, event_type: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Send one typed event through ``status_callback(kind="routing", json)``; never raises."""
    event = {"schema": SCHEMA_VERSION, "type": event_type, **payload}
    event["session_id"] = getattr(agent, "session_id", None)
    try:
        encoded = json.dumps(event, default=str, separators=(",", ":"))
    except Exception:
        logger.debug("routing event not serialisable: %s", event_type, exc_info=True)
        return None
    history = getattr(agent, "_routing_events", None)
    if isinstance(history, list):
        history.append(event)
        del history[:-50]
    else:
        agent._routing_events = [event]
    cb = getattr(agent, "status_callback", None)
    if cb:
        try:
            cb(ROUTING_STATUS_KIND, encoded)
        except Exception:
            logger.debug("status_callback error in routing event %s", event_type, exc_info=True)
    return event


def emit_provider_switched(agent: Any, *, reason: Any, old_model: str, old_provider: str,
                           new_model: str, new_provider: str, retry_eligible_in: Optional[int] = None) -> Optional[Dict[str, Any]]:
    return _emit(agent, "provider_switched", {
        "from_model": old_model, "from_provider": old_provider,
        "to_model": new_model, "to_provider": new_provider,
        "reason": reason_slug(reason), "reason_text": reason_text(reason),
        "retry_eligible_in_s": retry_eligible_in,
        "serving": serving_snapshot(agent),
    })


def emit_primary_restored(agent: Any, *, previous_model: str, previous_provider: str) -> Optional[Dict[str, Any]]:
    return _emit(agent, "primary_restored", {
        "from_model": previous_model, "from_provider": previous_provider,
        "to_model": str(getattr(agent, "model", "") or ""), "to_provider": str(getattr(agent, "provider", "") or ""),
        "serving": serving_snapshot(agent),
    })


def emit_account_switched(agent: Any, *, from_account: Optional[str], to_account: Optional[str],
                          reason: Any = None, cause: str = "rotation") -> Optional[Dict[str, Any]]:
    """``cause``: ``rotation`` (failure bench), ``refresh`` (pre-request resolver picked another
    pooled account), ``revert`` (benched account available again)."""
    if not to_account or from_account == to_account:
        return None
    return _emit(agent, "account_switched", {
        "provider": str(getattr(agent, "provider", "") or ""), "model": str(getattr(agent, "model", "") or ""),
        "from_account": from_account, "to_account": to_account,
        "reason": reason_slug(reason), "reason_text": reason_text(reason) if reason is not None else None,
        "cause": cause,
        "serving": serving_snapshot(agent),
    })


def emit_serving_if_changed(agent: Any) -> Optional[Dict[str, Any]]:
    """After a successful API call: publish the serving snapshot when it differs from the last one."""
    snap = serving_snapshot(agent)
    key = (snap["model"], snap["provider"], snap["account"], snap["fallback_active"],
           snap["reasoning"].get("effort"), snap["reasoning"].get("state"))
    if getattr(agent, "_last_serving_key", None) == key:
        return None
    agent._last_serving_key = key
    return _emit(agent, "serving", {"serving": snap})


def reset_serving_dedupe(agent: Any) -> None:
    agent._last_serving_key = None
