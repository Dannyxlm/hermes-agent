"""The card words every surface that cannot draw a card gets after the reply.

Reads the turn's own messages (an accepted ``show_card`` call and its ``ok`` receipt) and returns
each card's ``alt`` as its own paragraph: plain text, links intact, no JSON. BlueBubbles/Photon send
each paragraph as its own bubble, so one card is one bubble; Telegram and the other adapters format
it like any reply text.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from tools.cards.tool import MAX_CARDS_PER_TURN

_BLANK_LINES = re.compile(r"\n\s*\n+")


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def turn_cards(messages: Iterable[dict]) -> list[dict]:
    """Accepted cards in call order: ``{"card_id", "card", "alt"}`` for every ``show_card`` call whose
    tool row says ``ok: true``. A refused call, or one with no result yet, shows nothing anywhere."""
    calls: dict[str, dict] = {}
    order: list[str] = []
    accepted: dict[str, dict] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                function = (call or {}).get("function") or {}
                if function.get("name") == "show_card" and call.get("id"):
                    args = _loads(function.get("arguments"))
                    if isinstance(args, dict):
                        calls[call["id"]] = args
                        order.append(call["id"])
        elif message.get("role") == "tool" and message.get("tool_call_id") in calls:
            receipt = _loads(message.get("content"))
            if isinstance(receipt, dict) and receipt.get("ok") is True and isinstance(receipt.get("alt"), str):
                accepted[message["tool_call_id"]] = receipt
    return [{"card_id": call_id, "card": calls[call_id], "alt": accepted[call_id]["alt"]}
            for call_id in order if call_id in accepted][:MAX_CARDS_PER_TURN]


def cards_text(cards: list[dict]) -> str:
    """One paragraph per card; a card's own blank lines are folded so it stays one bubble."""
    paragraphs = [_BLANK_LINES.sub("\n", card["alt"]).strip() for card in cards]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def turn_card_text(agent_result: Any, history: list) -> str:
    """The card paragraphs for the turn in *agent_result* (only its new messages, not *history*)."""
    if not isinstance(agent_result, dict):
        return ""
    messages = agent_result.get("messages") or []
    offset = agent_result.get("history_offset", len(history or []))
    return cards_text(turn_cards(messages[offset:] if isinstance(offset, int) else messages))
