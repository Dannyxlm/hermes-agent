"""Provider-wide account preferences and read-only, identity-bound usage.

Never load_pool() on inventory/usage: that entry point seeds, heals and refreshes
credentials. Writes use native ordering under its canonical cross-process lock.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

PROVIDERS = {"anthropic": "Anthropic", "openai-codex": "Codex"}
_USAGE_TTL = 60
_USAGE_RETRY = 15
_USAGE_CAPACITY = 128
_USAGE_WORKERS = 4
_usage_lock = threading.RLock()
_usage_cache = {}
_usage_jobs = {}
_usage_epochs = {}
_usage_executor = ThreadPoolExecutor(max_workers=_USAGE_WORKERS, thread_name_prefix="account-usage")


class AccountControlError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _read_store(path):
    # The native loader repairs malformed JSON. Inventory must never repair it.
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise AccountControlError("Account inventory is temporarily unavailable.", 503) from None
    if not isinstance(data, dict):
        raise AccountControlError("Account inventory is temporarily unavailable.", 503)
    return data


@contextmanager
def _scope():
    from hermes_constants import get_hermes_home
    from hermes_cli import auth
    from plugins.cloudseed_mobile.scope import profile_id
    home = get_hermes_home().resolve()
    if auth._auth_file_path().resolve().parent != home:
        raise AccountControlError("Account profile could not be verified.", 403)
    yield profile_id(), home, auth


def _rows(store, provider):
    pools = store.get("credential_pool") or {}
    if not isinstance(pools, dict):
        raise AccountControlError("Account inventory needs repair before it can be changed.", 503)
    return pools.get(provider) or []


def _pool_state(provider, profile_id, home, auth):
    from agent.credential_pool import PooledCredential, _normalize_pool_priorities, get_pool_strategy
    own_path = home / "auth.json"
    owner = own_path
    store = _read_store(owner)
    rows = _rows(store, provider)
    if not rows:
        root = auth._global_auth_file_path()
        if root is not None:
            root_store = _read_store(root)
            root_rows = _rows(root_store, provider)
            if root_rows:
                owner, store, rows = root, root_store, root_rows
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str)
        or not row["id"] or len(row["id"]) > 200
        or not isinstance(row.get("priority", 0), int)
        for row in rows
    ) or len({row["id"] for row in rows}) != len(rows):
        raise AccountControlError("Account inventory needs repair before it can be changed.", 503)
    try:
        entries = [PooledCredential.from_dict(provider, row) for row in rows]
        _normalize_pool_priorities(provider, entries)
    except (TypeError, ValueError, AttributeError):
        raise AccountControlError("Account inventory needs repair before it can be changed.", 503) from None
    entries.sort(key=lambda entry: entry.priority)
    strategy = get_pool_strategy(provider)
    borrowed = owner.resolve() != own_path.resolve()
    reason = None
    if borrowed:
        reason = "These accounts are shared from the root profile. Open the root profile to change their order."
    elif strategy != "fill_first":
        reason = "This provider does not use fill first. Change its strategy in Hermes before choosing a primary account."
    elif not entries:
        reason = "No saved accounts. Add an account in Hermes first."
    revision = _digest({
        "profile": profile_id, "owner": str(owner.resolve()), "provider": provider,
        "generation": auth.read_pool_order_generation(provider, store), "strategy": strategy,
        "entries": [[e.id, e.priority, e.source] for e in entries],
    })
    return entries, rows, store, owner, strategy, reason, revision


def _can_move(provider, entries, account_id):
    from agent.credential_pool import _normalize_pool_priorities
    ordered = sorted(entries, key=lambda e: e.id != account_id)
    ordered = [replace(e, priority=i) for i, e in enumerate(ordered)]
    _normalize_pool_priorities(provider, ordered)
    return bool(ordered) and min(ordered, key=lambda e: e.priority).id == account_id


def _usage_key(home, provider, entry):
    # Tokens never leave the server. Include their digest so refresh/re-login
    # cannot reuse usage cached for a superseded credential with the same ID.
    return str(home), provider, entry.id, _digest([entry.access_token, entry.base_url])


def _cached_usage(home, provider, entry):
    key = _usage_key(home, provider, entry)
    with _usage_lock:
        cached = _usage_cache.get(key)
        job = _usage_jobs.get(key)
        loading = bool(job and job[0] == _usage_epochs.get(key[:2], 0))
        if cached:
            result = copy.deepcopy(cached[1])
            result["stale"] = bool(result.get("stale")) or time.monotonic() - cached[0] > _USAGE_TTL
            if loading:
                result["refreshing"] = True
            return result
    return {"status": "loading" if loading else "unknown", "fetched_at": None,
            "stale": False, "refreshing": loading, "windows": [],
            "message": "Usage has not been loaded."}


def _section(provider, profile_id, home, auth):
    from plugins.cloudseed_mobile.account_render import _entry_is_pool_exhausted, _entry_pool_retry_after, _safe_entry_label
    entries, rows, store, owner, strategy, reason, revision = _pool_state(provider, profile_id, home, auth)
    shared = owner.resolve() != (home / "auth.json").resolve() or auth._global_auth_file_path() is None
    accounts = []
    for i, entry in enumerate(entries):
        status = "dead" if entry.last_status == "dead" else "cooldown" if _entry_is_pool_exhausted(entry) else "ready"
        label = _safe_entry_label(entry, i + 1)
        if "@" in label or "sk-" in label or "Bearer " in label:
            label = f"Account {i + 1}"
        can_move = reason is None and status != "dead" and _can_move(provider, entries, entry.id)
        unavailable = reason
        if unavailable is None and status == "dead":
            unavailable = "Sign in to this account again before making it primary."
        if unavailable is None and not can_move:
            unavailable = "Hermes fixes this account's position behind manually added accounts."
        accounts.append({"id": entry.id, "label": label, "priority": i,
                         "is_primary": i == 0, "status": status,
                         "retry_after": _entry_pool_retry_after(entry),
                         "can_set_primary": can_move, "read_only_reason": unavailable,
                         "usage": _cached_usage(owner.parent.resolve(), provider, entry)})
    return {"id": provider, "display_name": PROVIDERS[provider], "strategy": strategy,
            "revision": revision, "scope": "shared_root" if shared else "profile",
            "scope_label": "Shared Ava accounts" if shared else "This Hermes profile",
            "can_set_primary": reason is None, "read_only_reason": reason,
            "primary_id": entries[0].id if entries else None, "accounts": accounts}


def get_accounts():
    with _scope() as (profile_id, home, auth):
        return {"ok": True, "profile_id": profile_id, "generated_at": _now(),
                "providers": [_section(p, profile_id, home, auth) for p in PROVIDERS]}


def _valid_provider(provider):
    if provider not in PROVIDERS:
        raise AccountControlError("Unsupported account provider.")
    return provider


def set_primary(body):
    if not isinstance(body, dict) or set(body) != {"provider", "account_id", "profile_id", "revision"}:
        raise AccountControlError("provider, account_id, profile_id and revision are required.")
    if any(not isinstance(v, str) or not v or len(v) > 256 for v in body.values()):
        raise AccountControlError("Invalid account preference request.")
    provider = _valid_provider(body["provider"])
    from agent.credential_pool import CredentialPool
    with _scope() as (profile_id, home, auth):
        if profile_id != body["profile_id"]:
            raise AccountControlError("Profile changed. Reload Accounts & Usage and try again.", 409)
        # Same canonical lock used by native writers, reentrant when move_entry persists.
        with auth._auth_store_lock(target_path=home / "auth.json"):
            entries, rows, store, owner, strategy, reason, revision = _pool_state(provider, profile_id, home, auth)
            if reason:
                raise AccountControlError(reason, 403)
            if revision != body["revision"]:
                raise AccountControlError("Account order changed. Reload Accounts & Usage and try again.", 409)
            selected = next((e for e in entries if e.id == body["account_id"]), None)
            if selected is None:
                raise AccountControlError("Account no longer exists. Reload Accounts & Usage.", 409)
            if selected.last_status == "dead" or not _can_move(provider, entries, selected.id):
                raise AccountControlError("This account cannot become the primary account.", 409)
            changed = entries[0].id != selected.id
            if changed:
                pool = CredentialPool(provider, entries)
                pool._persisted_token_pairs = auth._token_pairs_by_id(rows)
                pool._order_generation = auth.read_pool_order_generation(provider, store)
                pool.move_entry(selected.id, 0)
                after = _pool_state(provider, profile_id, home, auth)[0]
                if not after or after[0].id != selected.id:
                    raise AccountControlError("Hermes could not confirm the saved account order.", 409)
        if changed:
            _invalidate_usage(home, provider)
            # Native CredentialPool.move_entry persists the order generation.
            # Native pool readers reconcile that generation; no WebUI cache exists here.
        return get_accounts()


def _invalidate_usage(home, provider):
    identity = (str(home), provider)
    with _usage_lock:
        _usage_epochs[identity] = _usage_epochs.get(identity, 0) + 1
        # Reordering does not change the account identity or its quota. Keep
        # known windows; only older in-flight completions are fenced out.


def _probe_usage(provider, entry):
    """Use Hermes's usage HTTP/parsing primitives with THIS row's credential.

    No load_pool, resolver, refresh, fallback, quota reset or inference request.
    Expired credentials stay unknown until normal Hermes OAuth maintenance.
    """
    from agent import account_usage as usage
    from plugins.cloudseed_mobile.account_render import _decode_jwt_claims_unverified, _serialize_account_usage_snapshot
    token = entry.access_token
    if not token:
        raise ValueError("No usable saved token")
    if provider == "anthropic":
        if not usage._is_oauth_token(token):
            return {"status": "unavailable", "fetched_at": _now(), "stale": False,
                    "refreshing": False, "windows": [],
                    "message": "Usage is available only for Anthropic subscription accounts."}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20",
                   "User-Agent": "claude-code/2.1.0"}
        payload = usage._get_json("https://api.anthropic.com/api/oauth/usage", headers, timeout=5.0)
        windows = usage._usage_windows(payload, (("five_hour", "Current session"), ("seven_day", "Current week"),
                    ("seven_day_opus", "Opus week"), ("seven_day_sonnet", "Sonnet week")),
                    "utilization", "resets_at", fraction=False)
    else:
        # Only the official usage host receives subscription tokens. A proxy
        # credential is not an OAuth subscription and must never be forwarded.
        if entry.base_url and entry.base_url.rstrip("/") not in {
            "https://chatgpt.com/backend-api", "https://chatgpt.com/backend-api/codex"}:
            raise ValueError("Unsupported usage route")
        claims = _decode_jwt_claims_unverified(token).get("https://api.openai.com/auth") or {}
        account_id = claims.get("chatgpt_account_id") if isinstance(claims, dict) else None
        payload = usage._get_json(usage._codex_backend_urls("https://chatgpt.com/backend-api/codex")[0],
                                  usage._codex_headers(token, account_id), timeout=5.0)
        rate_limit = payload.get("rate_limit") or {}
        windows = usage._usage_windows(rate_limit, usage._codex_window_labels(rate_limit), "used_percent", "reset_at")
    snapshot = SimpleNamespace(provider=provider, windows=windows, fetched_at=datetime.now(timezone.utc))
    rendered = _serialize_account_usage_snapshot(snapshot)["windows"]
    for window in rendered:
        if not isinstance(window["used_percent"], (int, float)) or not math.isfinite(window["used_percent"]):
            window["used_percent"] = window["remaining_percent"] = None
    return {"status": "available" if rendered else "unavailable", "fetched_at": _now(),
            "stale": False, "refreshing": False, "windows": rendered,
            "message": None if rendered else "The provider did not return usage limits for this account."}


def _finish_probe(key, epoch, future):
    try:
        result = future.result()
    except Exception:
        # Provider exceptions can contain tokens or response bodies. Never echo them.
        result = {"status": "unavailable", "fetched_at": _now(), "stale": False,
                  "refreshing": False, "windows": [], "message": "Usage is temporarily unavailable. Try again shortly."}
    with _usage_lock:
        job = _usage_jobs.get(key)
        if job is None or job[1] is not future:
            return
        _usage_jobs.pop(key, None)
        if _usage_epochs.get(key[:2], 0) != epoch:
            return
        previous = _usage_cache.get(key)
        if result["status"] == "unavailable" and previous and previous[1]["status"] == "available":
            result = copy.deepcopy(previous[1])
            result["stale"] = True
            result["message"] = "Showing saved usage. Refresh is temporarily unavailable."
            _usage_cache[key] = (previous[0], result, time.monotonic())
        else:
            _usage_cache[key] = (time.monotonic(), result, time.monotonic())
        while len(_usage_cache) > _USAGE_CAPACITY:
            _usage_cache.pop(min(_usage_cache, key=lambda k: _usage_cache[k][0]))


def get_account_usage(provider, *, refresh=False):
    provider = _valid_provider(provider)
    with _scope() as (profile_id, home, auth):
        entries, _, _, owner, _, _, _ = _pool_state(provider, profile_id, home, auth)
        owner_home = owner.parent.resolve()
        jobs = []
        with _usage_lock:
            for entry in entries:
                key = _usage_key(owner_home, provider, entry)
                cached = _usage_cache.get(key)
                age = time.monotonic() - cached[2] if cached else float("inf")
                if age < (_USAGE_RETRY if refresh else _USAGE_TTL):
                    continue
                if key in _usage_jobs:
                    epoch, future = _usage_jobs[key]
                    jobs.append((key, epoch, future))
                elif len(_usage_jobs) < _USAGE_CAPACITY:
                    epoch = _usage_epochs.get(key[:2], 0)
                    future = _usage_executor.submit(_probe_usage, provider, entry)
                    _usage_jobs[key] = (epoch, future)
                    future.add_done_callback(lambda f, k=key, e=epoch: _finish_probe(k, e, f))
                    jobs.append((key, epoch, future))
        # Metadata GET is always instant; only this explicit usage route waits.
        if jobs:
            wait([future for _, _, future in jobs], timeout=20.0)
            # Future completion precedes callback execution. Publish completed
            # snapshots here as well; identity guard makes the callback a no-op
            # if this request wins, so a settled probe never remains "loading".
            for key, epoch, future in jobs:
                if future.done():
                    _finish_probe(key, epoch, future)
        return {"ok": True, "profile_id": profile_id, "generated_at": _now(),
                "providers": [_section(p, profile_id, home, auth) for p in PROVIDERS]}
