"""Bounded fast-mode windows (``/fast auto`` and ``/fast cold``).

``agent.service_tier``: ``None`` (normal), ``"priority"`` / ``"ultrafast"`` (static tiers,
pinned into ``agent.request_overrides`` at build time), ``"auto"`` (every user turn opens a
window of ``agent.fast_auto_seconds``) or ``"cold"`` (only a session's first turn,
no prior history, opens it). The provider's fast override is layered onto request
kwargs only while the window is open; only per-request params (``service_tier`` /
``speed``) vary, so the request body stays byte-identical. Anthropic keeps a separate
prompt cache per speed, so each Anthropic window boundary re-writes the prefix at the
new speed.
"""

from __future__ import annotations

import time
from typing import Any

BOUNDED_MODES = frozenset({"auto", "cold"})
DEFAULT_WINDOW_SECONDS = 60
# Documented fast-mode rate-limit headers; a limit of 0 means the organization has no fast
# capacity for the model (https://platform.claude.com/docs/en/build-with-claude/fast-mode).
_FAST_LIMIT_HEADERS = ("anthropic-fast-input-tokens-limit", "anthropic-fast-output-tokens-limit")
#: Tiers sent on every request of the session (OpenAI ``service_tier`` values; ``priority`` also
#: selects Anthropic/xAI fast mode). Ultrafast is OpenAI-only and gated per model.
STATIC_TIERS = frozenset({"priority", "ultrafast"})
NORMAL_TIER_WORDS = frozenset({"", "normal", "default", "standard", "off", "none"})
# User/config word -> agent.service_tier. The single table every surface (config loaders, /fast
# on CLI / gateway / TUI) parses through, so a new tier is one edit.
SERVICE_TIER_WORDS: dict[str, str] = {
    "fast": "priority", "priority": "priority", "on": "priority",
    "ultrafast": "ultrafast", "auto": "auto", "cold": "cold",
}


def parse_service_tier(raw: Any) -> str | None:
    """``agent.service_tier`` for a user/config word; None for normal and for unknown words."""
    value = str(raw or "").strip().lower()
    return None if value in NORMAL_TIER_WORDS else SERVICE_TIER_WORDS.get(value)


def service_tier_word(tier: Any) -> str:
    """The user-facing word for a stored tier (``priority`` -> ``fast``, None/"" -> ``normal``)."""
    return {"priority": "fast", None: "normal", "": "normal"}.get(tier, tier)


def begin_turn(agent: Any, conversation_history: Any) -> None:
    """Open (or refuse) the fast window at a user-turn boundary."""
    mode = getattr(agent, "service_tier", None)
    agent._fast_until = 0.0
    if mode not in BOUNDED_MODES:
        return
    if mode == "cold" and any(
        isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
        for m in (conversation_history or ())
    ):
        return
    try:
        window = float(getattr(agent, "fast_auto_seconds", DEFAULT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_SECONDS
    agent._fast_until = time.monotonic() + max(window, 0.0)


def effective_request_overrides(agent: Any) -> dict[str, Any]:
    """``agent.request_overrides`` plus the fast override while the window is open, minus
    ``speed`` for a model this session learned has no fast capacity."""
    overrides = dict(getattr(agent, "request_overrides", None) or {})
    if getattr(agent, "service_tier", None) in BOUNDED_MODES and time.monotonic() < getattr(agent, "_fast_until", 0.0):
        from hermes_cli.models import resolve_fast_mode_overrides
        base_url = getattr(agent, "base_url", None)
        if getattr(agent, "api_mode", None) == "anthropic_messages":
            base_url = getattr(agent, "_anthropic_base_url", None) or base_url
        overrides.update(
            resolve_fast_mode_overrides(getattr(agent, "model", None), provider=getattr(agent, "provider", None), base_url=base_url) or {}
        )
    if "speed" in overrides and getattr(agent, "model", None) in (getattr(agent, "_fast_mode_unavailable_models", None) or ()):
        overrides.pop("speed", None)
    return overrides


# The only values a fast override ever pins (see ``hermes_cli.models.resolve_fast_mode_overrides``).
_FAST_OVERRIDE_VALUES = {"speed": "fast", "service_tier": "priority"}


def rescope_fast_mode_after_fallback(agent: Any) -> None:
    """Re-resolve pinned fast overrides for the route a fallback just switched to.

    Static ``/fast`` pins the PRIMARY route's override (``speed: fast`` for Anthropic,
    ``service_tier: priority`` for OpenAI/xAI) into ``agent.request_overrides``. Carried
    across a fallback, that key reaches a provider that rejects it outright (Codex Responses
    "unsupported field(s): speed", OpenAI SDK "unexpected keyword argument 'speed'"), so a
    fast-mode rate limit kills every hop instead of degrading. Drop only fast values the new
    route does not produce; keep any other caller value of the same key (e.g. ``flex``), and
    re-pin the new route's own override while static fast mode is on. Bounded auto/cold windows
    need no pinning: ``effective_request_overrides`` resolves them per request.
    ``restore_primary_runtime`` restores the primary snapshot, so no reverse step is needed.
    """
    overrides = dict(getattr(agent, "request_overrides", None) or {})
    new_fast: dict[str, Any] = {}
    try:
        from hermes_cli.models import resolve_fast_mode_overrides

        base_url = getattr(agent, "base_url", None)
        if getattr(agent, "api_mode", None) == "anthropic_messages":
            base_url = getattr(agent, "_anthropic_base_url", None) or base_url
        new_fast = resolve_fast_mode_overrides(
            getattr(agent, "model", None), provider=getattr(agent, "provider", None), base_url=base_url
        ) or {}
    except Exception:
        new_fast = {}
    had_pinned_fast = False
    for key, fast_value in _FAST_OVERRIDE_VALUES.items():
        if overrides.get(key) == fast_value:
            had_pinned_fast = True
            if new_fast.get(key) != fast_value:
                overrides.pop(key, None)
    if had_pinned_fast and getattr(agent, "service_tier", None) == "priority":
        overrides.update(new_fast)
    agent.request_overrides = overrides


def fast_mode_unprovisioned(api_error: Any, api_kwargs: Any) -> bool:
    """True for a 429 on a ``speed: "fast"`` request whose fast-mode limit header is 0. The
    organization has no fast capacity for the model, so waiting or rotating keys cannot help."""
    if getattr(api_error, "status_code", None) != 429 or not isinstance(api_kwargs, dict):
        return False
    if (api_kwargs.get("extra_body") or {}).get("speed") != "fast":
        return False
    headers = getattr(getattr(api_error, "response", None), "headers", None)
    if headers is None:
        return False
    return any(str(headers.get(name, "")).strip() == "0" for name in _FAST_LIMIT_HEADERS)


# Anthropic bills fast mode only from usage credits, never from a Pro/Max plan's included
# allowance, and refuses an uncredited account with a plain 429 ``rate_limit_error`` that
# carries no fast-limit headers. Wordings: Messages API, then Claude Code's.
_FAST_CREDITS_REQUIRED_MARKERS = (
    "usage credits are required for fast mode",
    "fast mode requires usage credits",
)


def _api_error_text(api_error: Any) -> str:
    parts = [str(api_error)]
    body = getattr(api_error, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        parts.append(str(error.get("message", "")) if isinstance(error, dict) else str(body.get("message", "")))
    elif body is not None:
        parts.append(str(body))
    parts.append(str(getattr(api_error, "message", "") or ""))
    return " ".join(parts).lower()


def fast_mode_requires_credits(api_error: Any, api_kwargs: Any) -> bool:
    """True when a ``speed: "fast"`` request was refused because the account has no usage
    credits. An entitlement refusal, not a rate limit: retrying, waiting or benching the key
    cannot help, while the same request at standard speed is covered by the plan."""
    if not isinstance(api_kwargs, dict) or (api_kwargs.get("extra_body") or {}).get("speed") != "fast":
        return False
    status = getattr(api_error, "status_code", None)
    if status is not None and not 400 <= int(status) < 500:
        return False
    text = _api_error_text(api_error)
    return any(marker in text for marker in _FAST_CREDITS_REQUIRED_MARKERS)


def mark_fast_mode_unavailable(agent: Any) -> bool:
    """Stop sending ``speed`` for the current model for the rest of the session. False when the
    model was already marked, so the caller retries at most once per model."""
    model = getattr(agent, "model", None)
    unavailable = getattr(agent, "_fast_mode_unavailable_models", None)
    if not isinstance(unavailable, set):
        unavailable = agent._fast_mode_unavailable_models = set()
    if not model or model in unavailable:
        return False
    unavailable.add(model)
    return True
