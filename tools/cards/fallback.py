"""The card words every surface that cannot draw a card gets after the reply.

Reads the turn's own messages (an accepted ``show_card`` call and its ``ok`` receipt) and returns
each card's ``alt`` as its own paragraph: plain text, links intact, no JSON. BlueBubbles/Photon send
each paragraph as its own bubble, so one card is one bubble; Telegram and the other adapters format
it like any reply text.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Iterator

from tools.cards.tool import MAX_CARDS_PER_TURN

_BLANK_LINES = re.compile(r"\n\s*\n+")
logger = logging.getLogger(__name__)


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _card_calls(message: dict) -> Iterator[tuple[str, dict]]:
    """``(tool call id, card)`` for each well-formed ``show_card`` call in an assistant row."""
    calls = message.get("tool_calls")
    for call in calls if isinstance(calls, list) else ():
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get("name") != "show_card":
            continue
        call_id, args = call.get("id"), _loads(function.get("arguments"))
        if isinstance(call_id, str) and call_id and isinstance(args, dict):
            yield call_id, args


def turn_cards(messages: Iterable[Any]) -> list[dict]:
    """Accepted cards in call order: ``{"card_id", "card", "alt"}`` for every ``show_card`` call whose
    tool row says ``ok: true``. A refused call, or one with no result yet, shows nothing anywhere; a
    call id seen twice counts once (its first call)."""
    calls: dict[str, dict] = {}
    accepted: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call_id, card in _card_calls(message):
                calls.setdefault(call_id, card)
        elif message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str) and message["tool_call_id"] in calls:
            receipt = _loads(message.get("content"))
            alt = receipt.get("alt") if isinstance(receipt, dict) and receipt.get("ok") is True else None
            if isinstance(alt, str) and alt.strip():
                accepted.setdefault(message["tool_call_id"], alt)
    return [{"card_id": call_id, "card": card, "alt": accepted[call_id]}
            for call_id, card in calls.items() if call_id in accepted][:MAX_CARDS_PER_TURN]


def cards_text(cards: list[dict]) -> str:
    """One paragraph per card; a card's own blank lines are folded so it stays one bubble, and no
    words can turn into an attachment on the way out (``defang``)."""
    from tools.cards.alt import defang
    paragraphs = [defang(_BLANK_LINES.sub("\n", card["alt"]).strip()) for card in cards]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def this_turn(messages: list, offset: Any) -> list:
    """The turn's own messages. ``history_offset`` marks them, except after a mid-turn compaction,
    when the gateway reports 0 (the list is the compacted set) or the offset no longer fits: then the
    turn starts at its last user message (``gateway/media_repair.py`` recovers the turn the same way)."""
    if isinstance(offset, int) and not isinstance(offset, bool) and 0 < offset <= len(messages):
        return messages[offset:]
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], dict) and messages[index].get("role") == "user":
            return messages[index:]
    return messages if offset == 0 else []


def _mentions_show_card(messages: list) -> bool:
    return any(isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list)
               and "show_card" in json.dumps(m["tool_calls"], default=str) for m in messages)


def turn_card_text(agent_result: Any, history: list) -> str:
    """The card paragraphs for the turn in *agent_result* (only its new messages, not *history*).
    Every messaging reply passes through here: it is a no-op unless the turn called ``show_card``,
    and it never raises into a reply."""
    if not isinstance(agent_result, dict) or not isinstance(agent_result.get("messages"), list):
        return ""
    messages = agent_result["messages"]
    try:
        turn = this_turn(messages, agent_result.get("history_offset", len(history or [])))
        return cards_text(turn_cards(turn)) if _mentions_show_card(turn) else ""
    except Exception:  # health: allow BLE001 -- card words are optional; the reply must still go out
        logger.warning("card words skipped for this reply", exc_info=True)
        return ""
