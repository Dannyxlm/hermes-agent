"""Which connections draw Paper cards: ``client.capabilities {cards: {v, kinds}}``.

Recorded on the transport object itself, so it lives and dies with the connection (no registry to
forget on disconnect). A session's agent gets the ``cards`` toolset when any client attached to the
session at build time advertised the version this server speaks. A long-lived chat (Home) whose
agent was built before such a client attached is rebuilt once, at the next turn's admission
(``sync_cards_toolset``): the tool array changes between turns, never inside one (prompt caching).
Under ``dashboard.turn_isolation`` a turn may run in the compute-host child, which never sees the
client's socket: each turn frame carries ``CLIENT_DRAWS_CARDS`` from the serving process instead.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CARDS_VERSION = 1
_ATTRIBUTE = "cards_capability"
# Turn-frame field and child session key: the serving process's answer to ``session_draws_cards`` for a
# turn it hands to the compute host (``dashboard.turn_isolation``).
CLIENT_DRAWS_CARDS = "client_draws_cards"


def advertise(transport: Any, cards: dict | None) -> bool:
    """Record *transport*'s advertisement (``None`` withdraws it); False when it cannot carry one."""
    value = dict(cards) if isinstance(cards, dict) else None
    try:
        setattr(transport, _ATTRIBUTE, value)
    except (AttributeError, TypeError):  # __slots__ transports (stdio) have no reader that draws cards
        return False
    return True


def draws_cards(transport: Any) -> bool:
    peers = transport.transports() if hasattr(transport, "transports") else [transport]
    return any((getattr(peer, _ATTRIBUTE, None) or {}).get("v") == CARDS_VERSION for peer in peers)


def session_draws_cards(session: dict | None) -> bool:
    """A card-drawing client is attached to *session*: on its own transport, or, in the compute-host
    child (whose transport is the pipe to the serving process), as the serving process said on this
    turn's frame (``CLIENT_DRAWS_CARDS``)."""
    return bool(session) and (draws_cards(session.get("transport")) or session.get(CLIENT_DRAWS_CARDS) is True)


def _plus_cards(selection: list[str] | None) -> list[str]:
    if selection is not None and ("cards" in selection or not selection):  # [] is an explicit zero-tool state
        return selection
    if selection is None:
        from toolsets import TOOLSET_SESSION_PLATFORMS, get_all_toolsets
        selection = [name for name in get_all_toolsets() if name not in TOOLSET_SESSION_PLATFORMS]
    return [*selection, "cards"]


def with_cards_toolset(selection: list[str] | None, session: dict | None,
                       client_draws_cards: bool | None = None) -> list[str] | None:
    """*selection* plus ``cards`` for a session a card-drawing client is attached to.

    ``None`` (every toolset) never includes a platform-gated toolset such as ``cards``, so it is
    spelled out first. ``cards.enabled`` decides here, not only in the tool's ``check_fn``: a
    session's tool pin carries a pinned tool forward whenever the selection names its toolset,
    before ``check_fn`` is asked, so the flag must keep ``cards`` out of the selection to be an
    off switch for a chat that already showed one. A build that runs before the session record
    exists (the compute host's) passes ``client_draws_cards`` instead."""
    drawing = session_draws_cards(session) if client_draws_cards is None else client_draws_cards
    return _plus_cards(selection) if drawing and offered_version() else selection


def carry_cards_toolset(selection: list[str] | None, agent: Any) -> list[str] | None:
    """A tool refresh keeps the ``cards`` toolset the agent was built with (the tool array is part of
    the cached prefix; who is attached now does not change it), while ``cards.enabled`` is on."""
    built_with = "cards" in (getattr(agent, "enabled_toolsets", None) or ())
    return _plus_cards(selection) if built_with and offered_version() else selection


def offered_version() -> int:
    """The card contract version this server offers right now: 1 with ``cards.enabled``, else 0."""
    from tools.cards.tool import cards_enabled
    return CARDS_VERSION if cards_enabled() else 0


def sync_cards_toolset(sid: str, session: dict) -> bool:
    """Turn admission: rebuild the session's agent once when a card-drawing client is attached and
    the agent was built without ``show_card``. True when it rebuilt.

    Runs where the Bot Chat capability sync runs (``prompt_turn._prepare_turn_input``), so the chat
    is between turns and history (DB-backed, ``session["history"]``) carries over. One attempt per
    agent: a rebuild that still lacks the tool (profile disabled it, platform gate) is not retried
    every turn, and a client attaching again changes nothing once the agent has it."""
    agent = session.get("agent")
    toolsets = getattr(agent, "enabled_toolsets", None)
    if agent is None or toolsets == [] or "cards" in (toolsets or ()):  # [] is an explicit zero-tool state
        return False
    if session.get("cards_synced_agent") == id(agent) or not session_draws_cards(session):
        return False
    from tools.cards.tool import cards_enabled
    if not cards_enabled():
        return False
    from tui_gateway import server
    session["cards_synced_agent"] = id(agent)
    try:
        tokens = server._set_session_context(sid, cwd=server._session_cwd(session))
        try:
            new_agent = server._rebuild_session_agent(sid, session, **_live_settings(agent), session_id=session["session_key"],
                                                      platform_override=server._session_source(session))
        finally:
            server._clear_session_context(tokens)
        server._wire_session_agent(sid, session["session_key"], new_agent)  # memory-review summary, approvals
    except Exception:  # health: allow BLE001 -- the turn runs on the agent it has; cards wait for the next build
        logger.warning("cards toolset sync failed for %s", sid, exc_info=True)
        return False
    if getattr(agent, "_session_title_hint", None):
        new_agent._session_title_hint = agent._session_title_hint  # Bot Chat identity rides the prompt
    session["cards_synced_agent"] = id(new_agent)
    return True


def _live_settings(agent: Any) -> dict:
    """What the chat runs with now that a rebuild from config would lose: a resumed chat's stored
    reasoning effort and service tier arrive through ``resume_runtime_overrides``, not the session
    pins ``_rebuild_session_agent`` carries. ``""`` is "no tier", distinct from "read config"."""
    kept = {"service_tier_override": getattr(agent, "service_tier", None) or ""}
    if getattr(agent, "reasoning_config", None) is not None:
        kept["reasoning_config_override"] = agent.reasoning_config
    return kept
