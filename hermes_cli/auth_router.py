"""``hermes auth router``: read-only quota probes, shared decisions and safe status."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from agent.account_router.router import load_policy, ordered_ids
from agent.account_router.scorer import rank
from agent.account_router.snapshot import decode_meters, merge_meters, read_snapshot, snapshot_path

PROVIDERS = ("anthropic", "openai-codex", "xai-oauth")


def _entries(provider):
    from agent.credential_pool import PooledCredential, _normalize_pool_priorities
    from hermes_cli.auth import read_credential_pool
    entries = [PooledCredential.from_dict(provider, raw) for raw in read_credential_pool(provider)]
    _normalize_pool_priorities(provider, entries)
    return sorted(entries, key=lambda e: e.priority)


def _fetch(entry):
    # Late imports keep status usable without provider-specific dependencies.
    from agent.account_router import meters_anthropic
    fetchers = {"anthropic": meters_anthropic.fetch}
    if entry.provider not in fetchers:
        raise ValueError("No meter fetcher for this provider")
    return fetchers[entry.provider](entry)


def refresh(providers):
    from agent.memory_provider import ctx_bound
    policy = load_policy()
    result = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="account-router-refresh") as executor:
        jobs = [(provider, entries, [executor.submit(ctx_bound(_fetch), e) for e in entries])
                for provider in providers for entries in [_entries(provider)]]
        for provider, entries, futures in jobs:
            meters, unknown = [], 0
            for entry, future in zip(entries, futures):
                try:
                    meters.append(future.result())
                except Exception:  # health: allow BLE001 -- transport bodies/bearers must never enter logs; record unknown instead
                    unknown += 1  # Never print a transport exception: it may contain a body/token.
                    from agent.account_router.types import AccountMeter
                    meters.append(AccountMeter(provider, entry.id, None, (), time.time(), "usage_api"))
            merge_meters(snapshot_path(provider), meters, policy=policy,
                         entry_ids={provider: [e.id for e in entries]})
            result.append({"provider": provider, "updated": len(meters) - unknown, "unknown": unknown})
    return result


def status(providers):
    policy, now = load_policy(), time.time()
    result = []
    for provider in providers:
        entries = _entries(provider)
        state = read_snapshot(snapshot_path(provider))
        meters = decode_meters(state, provider)
        ids = [e.id for e in entries]
        scopes = ("all", "fable", "opus", "sonnet") if provider == "anthropic" else ("all",)
        pin = state.get("pins", {}).get(provider, {})
        for scope in scopes:
            model = None if scope == "all" else "claude-" + scope + "-router"
            scores = {id: (score, reason) for id, score, reason in rank(provider, model, ids, meters, now, policy)}
            order = ordered_ids(provider, model, ids, meters, state, now, policy)
            rows = [{"entry_id": id, "score": scores[id][0], "reason": scores[id][1],
                     "meter_age_seconds": max(0, now - meters[id].fetched_at) if id in meters else None,
                     "pinned": pin.get("entry_id") == id and pin.get("until", 0) > now}
                    for id in order]
            decision = state.get("decisions", {}).get(provider, {}).get(scope, {})
            result.append({"provider": provider, "scope": scope, "order": rows,
                           "decided_at": decision.get("decided_at"),
                           "pin": pin if pin.get("until", 0) > now else None})
    return result


def auth_router_command(args):
    providers = [args.provider] if args.provider else PROVIDERS
    action = args.router_action
    if action == "refresh":
        for row in refresh(providers):
            print(f"{row['provider']}: updated={row['updated']} unknown={row['unknown']}")
    else:
        rows = status(providers)
        if args.json:
            print(json.dumps(rows, allow_nan=False))
            return
        for row in rows:
            print(f"{row['provider']}/{row['scope']}:")
            for entry in row["order"]:
                print(f"  {entry['entry_id']} score={entry['score']} age={entry['meter_age_seconds']}s "
                      f"pin={entry['pinned']} ({entry['reason']})")
