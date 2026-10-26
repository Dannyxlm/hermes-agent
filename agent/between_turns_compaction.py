"""Eligibility and isolated Python bookkeeping for opportunistic post-turn compaction."""
from __future__ import annotations

import contextlib
import copy
import math
from typing import Any

from utils import is_truthy_value
from agent.conversation_compression import CompressionCommitFence


class SessionCompactionFence(CompressionCommitFence):
    """Extend the core commit fence through the host's history/version publication."""

    def __init__(self, session, version):
        super().__init__()
        self.session = session
        self.version = version
        self.publication_locked = False

    def begin_commit(self, cancel_event=None):
        lock = self.session["history_lock"]
        lock.acquire()
        if (self.session.get("running")
                or int(self.session.get("history_version", 0)) != self.version):
            self.cancel_before_commit(reason="snapshot_stale")
        if not super().begin_commit(cancel_event):
            lock.release()
            return False
        self.publication_locked = True
        return True

    def release_publication(self):
        if self.publication_locked:
            self.publication_locked = False
            self.session["history_lock"].release()



def _copy_state(values: dict) -> dict:
    """Copy Python bookkeeping, not clients, stores, callbacks or synchronization resources."""
    memo = {}
    seen = set()
    def resources(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if type(value) in (dict, list, tuple, set, frozenset):
            for item in (value.values() if type(value) is dict else value):
                resources(item)
        elif type(value) not in (str, int, float, bool, bytes, type(None)):
            memo[id(value)] = value
    resources(values)
    return copy.deepcopy(values, memo)


def _state_stamp(value, seen=None):
    """Compare bookkeeping without invoking SDK/plugin equality methods."""
    if type(value) in (str, int, float, bool, bytes, type(None)):
        return value
    if type(value) not in (dict, list, tuple, set, frozenset):
        return id(value)
    seen = set() if seen is None else seen
    if id(value) in seen:
        return "recursive"
    seen.add(id(value))
    if type(value) is dict:
        return tuple((k, _state_stamp(v, seen)) for k, v in value.items())
    items = tuple(_state_stamp(v, seen) for v in value)
    return frozenset(items) if type(value) in (set, frozenset) else items


def fork_compaction_agent(agent):
    """A detached summarizer must own neither the live agent nor its compressor state.

    The core still performs the *same* automatic commit, on the same DB and session.
    Only its Python bookkeeping is private until that commit wins the fence.
    """
    from agent.context_compressor import ContextCompressor
    from agent.context_engine import ContextEngine

    shadow = copy.copy(agent)
    shadow.__dict__ = _copy_state(dict(vars(agent)))
    # Core's Anthropic credential refresh closes its old client before rebuilding.
    # The speculative owner borrows no closable main client; a refreshed client is
    # owned by this fork until publication (or discarded by the worker on abort).
    if "_anthropic_client" in vars(shadow):
        shadow._anthropic_client = None
    engine = agent.context_compressor
    if isinstance(engine, ContextEngine) and type(engine) is not ContextCompressor:
        # Plugin engines already define their own resource-aware isolation contract.
        compressor = engine.clone_for_agent()
        if compressor is engine:
            raise ValueError("between-turns compaction requires an isolated context engine")
    else:
        compressor = copy.copy(engine)
        compressor.__dict__ = _copy_state(dict(vars(engine)))
    # A fresh attempt owns its serial lock / cancellation registration; never inherit
    # the live object's fence stack, which a simultaneous manual call could mutate.
    for key in ("_compression_attempt_serial_lock", "_compression_cancelled_check"):
        vars(compressor).pop(key, None)
    shadow.context_compressor = compressor
    for key in ("_compression_commit_fence_lock", "_compression_commit_fence_stack", "_active_compression_commit_fence"):
        vars(shadow).pop(key, None)
    return shadow, {key: _state_stamp(value) for key, value in vars(shadow).items()}


def publish_compaction_agent(agent, shadow, baseline):
    """Publish only compression's delta, never unrelated background-review bookkeeping."""
    old_client = getattr(agent, "_anthropic_client", None)
    for key, value in vars(shadow).items():
        if key.startswith("_compression_commit_fence") or key == "_active_compression_commit_fence":
            continue
        if key == "context_compressor" or key not in baseline or _state_stamp(value) != baseline[key]:
            setattr(agent, key, value)
    if old_client is not None and getattr(agent, "_anthropic_client", None) is not old_client:
        with contextlib.suppress(Exception):
            old_client.close()


def discard_compaction_agent(shadow):
    """Close only a fork-owned refreshed client, never the live agent's client."""
    client = getattr(shadow, "_anthropic_client", None)
    if client is not None:
        with contextlib.suppress(Exception):
            client.close()


def abandon_compaction_agent(shadow):
    """Poison late compressor mutations and remove its durable cooldown write capability."""
    from agent.conversation_compression import _COMPRESSOR_ATTEMPT_LOCK
    cc = shadow.context_compressor
    # Never wait on the discarded attempt's serial rollback lock. New turns own
    # a different compressor; this generation is only a poison for late work.
    cc._session_db = None
    with _COMPRESSOR_ATTEMPT_LOCK:
        generation = int(getattr(cc, "_compression_attempt_generation", 0) or 0) + 1
        cc._compression_attempt_generation = generation
        cc._compression_working_attempt_generation = generation


def between_turns_settings(config: dict) -> tuple[bool, float]:
    """Construction and live reload share normalization, including unset semantics."""
    raw = config.get("between_turns_ratio", 0.9)
    try:
        ratio = float(raw)
        if isinstance(raw, bool) or not math.isfinite(ratio) or not 0 < ratio < 1:
            raise ValueError
    except (TypeError, ValueError):
        ratio = 0.9
    return is_truthy_value(config.get("between_turns", False)), ratio


def between_turns_pressure(agent: Any, messages: list) -> int | None:
    """Reuse preflight's route/usage anchor and idle's observed-output anti-thrash floor."""
    from agent import turn_context as tc

    if not (getattr(agent, "compression_between_turns", False) is True
            and getattr(agent, "compression_enabled", False) and messages):
        return None
    # App-server compaction runs inside its provider-owned thread/commit, even
    # in Hermes-initiated mode; it cannot be detached as a speculative summary.
    if getattr(agent, "api_mode", None) == "codex_app_server":
        return None
    cc = agent.context_compressor
    if (getattr(cc, "awaiting_real_usage_after_compression", False)
            or cc.get_active_compression_failure_cooldown()):
        return None
    tokens = tc._preflight_request_tokens(agent, messages, getattr(agent, "_cached_system_prompt", "") or "")
    # Speculative character counts must not launch a cache-breaking summarizer.
    if not getattr(agent, "_request_pressure_anchored", False):
        return None
    if not cc.threshold_tokens * agent.compression_between_turns_ratio <= tokens < cc.threshold_tokens:
        return None
    last = getattr(cc, "last_compression_rough_tokens", 0)
    last = last if type(last) is int else 0
    if not tc._should_idle_compact(
        enabled=True, idle_after_seconds=1, idle_gap_seconds=1, tokens=tokens,
        floor_tokens=int(cc.threshold_tokens * cc.summary_target_ratio),
        cooldown_active=False, last_compaction_tokens=last,
    ):
        return None
    return tokens
