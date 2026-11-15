"""Which connections draw Paper cards: ``client.capabilities {cards: {v, kinds}}``.

Recorded on the transport object itself, so it lives and dies with the connection (no registry to
forget on disconnect). A session's agent gets the ``cards`` toolset when any client attached to the
session at build time advertised the version this server speaks; the tool list is then fixed for
that agent's life (prompt caching), like every other toolset.
"""
from __future__ import annotations

from typing import Any

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
