"""Profile/pool-owner scoped, atomic sidecar transactions and mtime-cached reads."""
from __future__ import annotations

import logging
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .types import AccountMeter, MeterWindow

logger = logging.getLogger(__name__)
_cache = {}
_cache_lock = threading.RLock()
_holders = {}


def _version(path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino
    except FileNotFoundError:
        return None


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def _cached_json(path):
    path = Path(path)
    version = _version(path)
    with _cache_lock:
        cached = _cache.get(path)
        if cached is not None and cached[0] == version:
            return cached[1]
        data = _read_json(path) if version is not None else {}
        # Bound retention across short-lived profiles in a multiplex gateway.
        if len(_cache) >= 128:
            _cache.clear()
        _cache[path] = version, data
        return data


def snapshot_path(provider):
    from hermes_constants import get_hermes_home
    from hermes_cli.auth import _global_auth_file_path
    home = get_hermes_home()
    root_auth = _global_auth_file_path()
    if root_auth is not None:
        own = _cached_json(home / "auth.json").get("credential_pool", {}).get(provider)
        if not own:
            # Match read_credential_pool's per-provider fallback, never copy root tokens.
            home = root_auth.parent
    return home / "state" / "account_router.json"


def read_snapshot(path, *, cached=True):
    state = _cached_json(path) if cached else _read_json(path)
    if state and state.get("version") != 1:
        raise ValueError("Unsupported account router snapshot")
    return state


def decode_meters(state, provider):
    return {id: AccountMeter(provider=provider, entry_id=id, plan=raw.get("plan"),
                             windows=tuple(MeterWindow(**w) for w in raw["windows"]),
                             fetched_at=raw["fetched_at"], source=raw["source"], extras=raw.get("extras", {}))
            for id, raw in state.get("meters", {}).get(provider, {}).items()}


def _transaction(path, change):
    from hermes_cli.auth import _file_lock
    from utils import atomic_json_write
    path = Path(path)
    with _cache_lock:
        holder = _holders.setdefault(path, threading.local())
    with _file_lock(path.with_suffix(".lock"), holder, 5, "Account router snapshot busy"):
        state = read_snapshot(path, cached=False)
        state.setdefault("version", 1)
        if change(state) is False:
            return state
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, state, mode=0o600, allow_nan=False)
        with _cache_lock:
            _cache.pop(path, None)
    return state


def _merge_meter(previous, meter, *, partial):
    """Each window keeps its own observation time, even across out-of-order sources."""
    from .scorer import window_key
    if previous is None:
        raw = asdict(meter)
        raw["extras"]["window_fetched_at"] = {window_key(w): meter.fetched_at for w in meter.windows}
        raw["extras"]["header_captured_at" if partial else "usage_fetched_at"] = meter.fetched_at
        return raw
    usage_at = previous.extras.get("usage_fetched_at", previous.fetched_at if previous.source == "usage_api" else 0)
    if not partial and meter.fetched_at < usage_at:
        return asdict(previous)  # A newer complete fetch already superseded absent windows too.
    old_stamps = previous.extras.get("window_fetched_at", {})
    stamps = {window_key(w): old_stamps.get(window_key(w), previous.fetched_at) for w in previous.windows}
    windows = {window_key(w): w for w in previous.windows
               if partial or stamps[window_key(w)] > meter.fetched_at}
    for window in meter.windows:
        key = window_key(window)
        if stamps.get(key, 0) <= meter.fetched_at:
            windows[key] = window
            stamps[key] = meter.fetched_at
    extras = dict(previous.extras)
    plan = previous.plan
    if not partial:
        extras.update(meter.extras)
        extras["usage_fetched_at"] = meter.fetched_at
        plan = meter.plan
    if partial:
        extras["header_captured_at"] = meter.fetched_at
    extras["window_fetched_at"] = {key: stamps[key] for key in windows}
    latest = max(previous.fetched_at, meter.fetched_at)
    source = meter.source if meter.fetched_at >= previous.fetched_at else previous.source
    return asdict(AccountMeter(meter.provider, meter.entry_id, plan, tuple(windows.values()), latest, source, extras))


def merge_meters(path, meters, *, policy, now=None, entry_ids=None, labels=None, partial=False):
    """Merge windows by observation time; full fetches retain only newer stored windows."""
    from .router import update_decisions
    now = time.time() if now is None else now
    def change(state):
        providers = set()
        for meter in meters:
            rows = state.setdefault("meters", {}).setdefault(meter.provider, {})
            old = rows.get(meter.entry_id)
            if partial and old and meter.fetched_at - old.get("extras", {}).get("header_captured_at", 0) < 60:
                continue
            previous = decode_meters(state, meter.provider).get(meter.entry_id)
            raw = _merge_meter(previous, meter, partial=partial)
            if raw == old:
                continue
            rows[meter.entry_id] = raw
            providers.add(meter.provider)
        if not providers:
            return False
        for provider in providers:
            saved = state.setdefault("entries", {}).setdefault(provider, [])
            ids = entry_ids.get(provider) if entry_ids is not None else None
            if ids is not None:
                state["entries"][provider] = list(ids)
            else:
                ids = saved + [id for id in state["meters"][provider] if id not in saved]
                state["entries"][provider] = ids
            update_decisions(state, provider, ids, decode_meters(state, provider), now, policy,
                             (labels or {}).get(provider))
    return _transaction(path, change)


def pin(provider, entry_id, *, path=None, now=None, policy=None):
    from .router import load_policy
    policy = load_policy() if policy is None else policy
    now = time.time() if now is None else now
    def change(state):
        state.setdefault("pins", {})[provider] = {"entry_id": entry_id,
                                                  "until": now + policy.get("pin_hours", 6) * 3600}
    return _transaction(snapshot_path(provider) if path is None else path, change)


def pin_best_effort(provider, entry_id, *, strategy, path=None):
    """A failed advisory pin must never fail an already-saved operator preference."""
    if strategy != "smart":
        return
    try:
        pin(provider, entry_id, path=path)
    except Exception:
        logger.debug("Account router pin unavailable; account order was saved", exc_info=True)
