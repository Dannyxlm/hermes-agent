"""Which connections draw Paper cards: ``client.capabilities {cards: {v, kinds}}``.

Recorded on the transport object itself, so it lives and dies with the connection (no registry to
forget on disconnect). A session's agent gets the ``cards`` toolset when any client attached to the
session at build time advertised the version this server speaks. A long-lived chat (Home) whose
agent was built before such a client attached is rebuilt once, at the next turn's admission
(``sync_cards_toolset``): the tool array changes between turns, never inside one (prompt caching).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CARDS_VERSION = 1
_ATTRIBUTE = "cards_capability"


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
    return bool(session) and draws_cards(session.get("transport"))


def _plus_cards(selection: list[str] | None) -> list[str]:
    if selection is not None and ("cards" in selection or not selection):  # [] is an explicit zero-tool state
        return selection
    if selection is None:
        from toolsets import TOOLSET_SESSION_PLATFORMS, get_all_toolsets
        selection = [name for name in get_all_toolsets() if name not in TOOLSET_SESSION_PLATFORMS]
    return [*selection, "cards"]


def with_cards_toolset(selection: list[str] | None, session: dict | None) -> list[str] | None:
    """*selection* plus ``cards`` for a session a card-drawing client is attached to.

    ``None`` (every toolset) never includes a platform-gated toolset such as ``cards``, so it is
    spelled out first; ``cards.enabled`` still gates the tool itself through its ``check_fn``."""
    return _plus_cards(selection) if session_draws_cards(session) else selection


def carry_cards_toolset(selection: list[str] | None, agent: Any) -> list[str] | None:
    """A tool refresh keeps the ``cards`` toolset the agent was built with (the tool array is part of
    the cached prefix; who is attached now does not change it)."""
    return _plus_cards(selection) if "cards" in (getattr(agent, "enabled_toolsets", None) or ()) else selection


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
            new_agent = server._rebuild_session_agent(sid, session, session_id=session["session_key"],
                                                      platform_override=server._session_source(session))
        finally:
            server._clear_session_context(tokens)
    except Exception:  # health: allow BLE001 -- the turn runs on the agent it has; cards wait for the next build
        logger.warning("cards toolset sync failed for %s", sid, exc_info=True)
        return False
    if getattr(agent, "_session_title_hint", None):
        new_agent._session_title_hint = agent._session_title_hint  # Bot Chat identity rides the prompt
    session["cards_synced_agent"] = id(new_agent)
    return True
