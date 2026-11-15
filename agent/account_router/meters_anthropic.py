"""Read-only OAuth meters and bounded, asynchronous Anthropic header updates."""
from __future__ import annotations

import math
import logging

import httpx
import threading
import time

from .types import AccountMeter, MeterWindow

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
logger = logging.getLogger(__name__)
_header_lock = threading.Lock()
_header_last = {}
_header_pending = set()


def _percent(value, *, fraction=False):
    try:
        value = float(value)
        if fraction:
            value *= 100
        return min(100, value) if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


def _epoch(value):
    from agent.account_usage import _parse_dt
    try:
        if isinstance(value, str):
            try:
                numeric = float(value)
                return numeric if math.isfinite(numeric) else None
            except ValueError:
                pass
        parsed = _parse_dt(value)
        return parsed.timestamp() if parsed else None
    except (ValueError, TypeError, OverflowError):
        return None


def _scope(value):
    if isinstance(value, dict):
        value = value.get("display_name") or value.get("name") or value.get("id")
    text = str(value or "").lower()
    return next((key for key in ("fable", "opus", "sonnet") if key in text), text or None)


def parse_usage(payload):
    windows = []
    for limit in payload.get("limits") or []:
        kind = limit.get("kind")
        normalized = {"session": "session", "weekly_all": "weekly", "weekly_scoped": "weekly",
                      "weekly": "weekly", "period": "period"}.get(kind)
        if normalized is None:
            continue
        scope = _scope((limit.get("scope") or {}).get("model")) if kind == "weekly_scoped" else None
        # An unidentified model-scoped meter is not an account-wide cap.
        if kind == "weekly_scoped" and scope is None:
            scope = "unknown-model"
        windows.append(MeterWindow(normalized, _percent(limit.get("percent")),
                                   _epoch(limit.get("resets_at")), scope))
    if not windows:
        for key, kind, scope in (("five_hour", "session", None), ("seven_day", "weekly", None),
                                 ("seven_day_opus", "weekly", "opus"),
                                 ("seven_day_sonnet", "weekly", "sonnet"),
                                 ("seven_day_fable", "weekly", "fable")):
            raw = payload.get(key)
            if isinstance(raw, dict):
                windows.append(MeterWindow(kind, _percent(raw.get("utilization")),
                                           _epoch(raw.get("resets_at")), scope))
    return tuple(windows)


def fetch(entry):
    from agent.account_usage import _get_json
    from .snapshot import decode_meters, read_snapshot, snapshot_path
    now = time.time()
    headers = {"Authorization": f"Bearer {entry.access_token}", "Accept": "application/json",
               "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"}
    cached = decode_meters(read_snapshot(snapshot_path("anthropic")), "anthropic").get(entry.id)
    plan = cached.plan if cached else None
    profile_at = cached.extras.get("profile_fetched_at", 0) if cached else 0
    if now - profile_at >= 86400 or not plan:
        try:
            profile = _get_json(_PROFILE_URL, headers, timeout=5.0)
            plan = (profile.get("organization") or {}).get("rate_limit_tier")
            profile_at = now
        except (httpx.HTTPError, ValueError, TypeError, OSError):
            # No transport traceback: it may contain a provider body or bearer.
            profile_at = 0
    payload = _get_json(_USAGE_URL, headers, timeout=5.0)
    return AccountMeter("anthropic", entry.id, plan, parse_usage(payload), now, "usage_api",
                        {"profile_fetched_at": profile_at})


def parse_headers(headers):
    lowered = {str(k).lower(): v for k, v in headers.items()}
    windows = []
    for tag, kind, scope in (("5h", "session", None), ("7d", "weekly", None),
                              ("7d-fable", "weekly", "fable"), ("7d-opus", "weekly", "opus"),
                              ("7d-sonnet", "weekly", "sonnet")):
        prefix = "anthropic-ratelimit-unified-" + tag
        used = _percent(lowered.get(prefix + "-utilization"), fraction=True)
        if used is not None:
            windows.append(MeterWindow(kind, used, _epoch(lowered.get(prefix + "-reset")), scope))
    return tuple(windows)


def capture_headers(agent, headers):
    """Start at most one write per minute per owning-home/entry; never wait on disk."""
    if getattr(agent, "provider", None) != "anthropic":
        return
    pool = getattr(agent, "_credential_pool", None)
    if getattr(pool, "_strategy", None) != "smart":
        return
    id = getattr(agent, "_credential_pool_entry_id", None)
    windows = parse_headers(headers)
    if not id or not windows:
        return
    from hermes_constants import get_hermes_home
    from agent.memory_provider import spawn_context_thread
    key = (str(get_hermes_home()), id)
    now = time.time()
    with _header_lock:
        if key in _header_pending or now - _header_last.get(key, 0) < 60:
            return
        _header_pending.add(key)
        _header_last[key] = now
    def write():
        try:
            from .snapshot import merge_meters, snapshot_path
            from .router import load_policy
            merge_meters(snapshot_path("anthropic"),
                         [AccountMeter("anthropic", id, None, windows, now, "headers")],
                         policy=load_policy(), now=now, partial=True)
        except Exception:
            logger.debug("Account router header sidecar unavailable", exc_info=True)
        finally:
            with _header_lock:
                _header_pending.discard(key)
    try:
        spawn_context_thread(write, name="account-router-headers").start()
    except Exception:
        logger.debug("Account router header worker unavailable", exc_info=True)
        with _header_lock:
            _header_pending.discard(key)
