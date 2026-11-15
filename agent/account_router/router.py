"""Read-only selection; durable hysteresis is decided by snapshot writers."""
from __future__ import annotations

import logging
import re
import time

from .scorer import DEFAULT_POLICY, rank, scope_key

logger = logging.getLogger(__name__)


def load_policy():
    # Unlike model config, routing policy intentionally hot-reloads in frozen sessions.
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable
    try:
        return load_config_readonly().get("account_router", DEFAULT_POLICY)
    except (Exception, ConfigBackendUnavailable):
        logger.debug("Account router policy unavailable", exc_info=True)
        return DEFAULT_POLICY


def decide(provider, model, ids, meters, previous, now, policy):
    rows = rank(provider, model, ids, meters, now, policy)
    order = [row[0] for row in rows]
    if not order or all(row[1] is None for row in rows):
        changed = next(iter(previous.get("order", [])), None) != next(iter(ids), None)
        return {"order": ids, "decided_at": now if changed else previous.get("decided_at", now)}
    old = next(iter(previous.get("order", [])), None)
    scores = {id: score for id, score, _ in rows}
    held = old in order and scores[old] != 0
    if held and old != order[0]:
        challenger, current = scores[order[0]], scores[old]
        dwell = now - previous.get("decided_at", now)
        beats_margin = challenger is not None and (current is None or
                        challenger >= current * (1 + policy.get("switch_margin", .25)))
        if dwell < policy.get("min_dwell_minutes", 120) * 60 or not beats_margin:
            order.remove(old)
            order.insert(0, old)
    changed = old != order[0]
    return {"order": order, "decided_at": now if changed else previous.get("decided_at", now)}


def ordered_ids(provider, model, ids, meters, state, now, policy):
    previous = state.get("decisions", {}).get(provider, {}).get(scope_key(model) or "all", {})
    # Only writers advance the durable dwell clock. Selection may bypass an unavailable
    # or capped top, but never starts an unrecorded healthy-account switch.
    order = decide(provider, model, ids, meters, previous, now,
                   {**policy, "min_dwell_minutes": float("inf")})["order"]
    pin = state.get("pins", {}).get(provider, {})
    pinned = pin.get("entry_id")
    if pin.get("until", 0) > now and pinned in order:
        order.remove(pinned)
        order.insert(0, pinned)
    return order


def reorder(provider, model, entries):
    """No network, locks or writes. On any read/parse failure retain static priority."""
    from .snapshot import read_snapshot, snapshot_path, decode_meters
    try:
        state = read_snapshot(snapshot_path(provider))
        meters = decode_meters(state, provider)
        ids = ordered_ids(provider, model, [e.id for e in entries], meters, state, time.time(), load_policy())
        by_id = {e.id: e for e in entries}
        return [by_id[id] for id in ids]
    except Exception:
        logger.debug("Account router snapshot unavailable", exc_info=True)
        return entries


def update_decisions(state, provider, ids, meters, now, policy, labels=None):
    saved_labels = state.setdefault("labels", {}).setdefault(provider, {})
    for id, label in (labels or {}).items():
        if (isinstance(label, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9 ._()-]{0,39}", label)
                and not label.lower().startswith(("sk-", "bearer "))
                and (len(label) <= 20 or " " in label)):
            saved_labels[id] = label
    decisions = state.setdefault("decisions", {}).setdefault(provider, {})
    scopes = {"all", "fable", "opus", "sonnet"} if provider == "anthropic" else {"all"}
    for scope in sorted(scopes):
        previous = decisions.get(scope, {})
        model = "claude-" + scope + "-router" if scope != "all" else None
        decision = decide(provider, model, ids, meters, previous, now, policy)
        old = next(iter(previous.get("order", [])), None)
        new = next(iter(decision["order"]), None)
        decisions[scope] = decision
        if old != new:
            labels = saved_labels
            logger.info("account router: %s/%s top %s -> %s (%s)", provider, scope,
                        labels.get(old, old), labels.get(new, new), "allowance urgency / dwell")
