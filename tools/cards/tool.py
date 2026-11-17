"""``show_card``: Ava shows a card under her reply in the Hermes app.

The card is the call's arguments (data, never code; ``card.schema.json``). The handler validates and
refuses with paths the model can repair, derives the plain-words ``alt``, and returns a small receipt.
Nothing a card holds causes an effect on the server.

Offered only where someone can draw it: ``cards.enabled`` in config (``check_fn``), a session on a
local chat platform (the toolset's ``platforms``), and a client that advertised ``cards: {v: 1}`` in
``client.capabilities`` when the session's agent was built (``tui_gateway/cards_capability.py``). Once
offered, the chat's pinned tool array keeps it on turns that client isn't on (a wake, a thread update)
while the flag is on, so the array does not flip (``tools/mcp_tool_agent.py::_carried_card_names``).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
from collections import OrderedDict
from typing import Any, Optional

from tools.cards.normalize import normalize_card
from tools.registry import registry

MAX_CARDS_PER_TURN = 6
_TURNS_REMEMBERED = 512
_turn_counts: OrderedDict[tuple[str, str], int] = OrderedDict()
_turn_lock = threading.Lock()
# Said to the model, never to Danny: the switch is plumbing, so the reply just answers (Iris F6).
CARDS_OFF = "no cards for now; give the answer in words and don't mention cards or this result"

_DESCRIPTION = (
    "Show Danny a card under your reply in the Hermes app: a natively drawn object (a sheet, a flight, a "
    "route, a place, the weather, a chart, a document, a link, photos, or a short composed summary). Use it "
    "when the thing itself is better seen than read: options to compare (table), an itinerary (flight), "
    "getting somewhere (route), one spot (place), a forecast (weather), a trend (chart), a file to look at "
    "(document), a page (link), images (gallery). Use compose for a summary that mixes stats, a timeline and "
    "rows. Not for a plain answer, code, or anything two sentences already make clear. You have this tool because "
    "he reads this chat in the app, which draws the card under this reply even when the session is described "
    "as a terminal: never tell him where cards appear.\n"
    "Rules: your reply must stand on its own. Say the answer in a sentence or two; the card shows the thing "
    "itself (notifications, older apps and other chats see only your words and the card's alt). He sees the "
    "card, so your words carry the point and never repeat it: no markdown copy of its table, no list of its "
    "rows, and the note doesn't restate your sentence. Data only: "
    "no sizes, colours or layout, the app owns the look. title ≤ 80 chars. note is optional: one sentence, "
    "≤ 240 chars, on what it means for him. At most two actions; ask only fills his composer with its exact "
    "text, it never sends. Files are absolute paths on this server; never web image URLs. compose needs alt "
    "(plain words, ≤ 1,200 chars); other kinds derive it. At most six cards a turn; several cards go as "
    "parallel calls. If a call is refused, fix each listed path and call again, or drop the card: a refusal "
    "is yours to correct quietly, so never describe the card's mechanics, refusals or retries to him. Fill the "
    "optional fields you know: they are what the app draws (a flight's city names, seat line and airport coordinates for the "
    "arc; a document's quotes with their pages; a route stop's address)."
)

_DATA_DESCRIPTION = (
    "The kind's fields. table: {columns:[{title, type?: text|number|money|percent|date|delta|status, "
    "currency?}] (1-12), rows:[{cells:[one per column: string|number|null], secondary?, badge?, tone?, "
    "highlight?}] (≤60), totals?: [one cell per column, like a row's cells], count?, file?}; percent cells are "
    "fractions (0.541 shows as 54.1%, 1.5 as 150%); variant sheet is the full-sheet look. flight: {legs:[{number?, "
    "from, to (airport codes), departure, arrival (text, e.g. \"17:55\"), dayOffset?, duration? (text, e.g. \"6 h 45\"), "
    "date?, fromName?, toName? (cities), "
    "detail? (seat line), arrivalDetail?, fromLat?/fromLon?, toLat?/toLon? (pairs)}] (1-6), connection?, "
    "connectionCode?, airline?, total?, price? (text with its currency, e.g. \"$1,482\"), priceNote?, leave?: {time, "
    "label?, detail?}}. route: {stops:[{name, lat, lon, address?, detail?}] (2-12), mode?: drive|walk|transit|cycle|"
    "flight, duration? (text, e.g. \"24 min by car\"), distance? (text, e.g. \"21 km\")}. place: "
    "{name, lat, lon, address?, detail?}. weather: {headline (now, e.g. \"4°\"), condition?: clear|partly_cloudy|cloudy|"
    "rain|snow|storm|fog|wind, comparison?, range?, chart?: <chart data>, days?:[{day, high?, low?, condition?}] "
    "(≤10)}; headline, high and low are temperatures: a number shows as 7°, with the card's °C or °F if it states one. "
    "chart: {type?: line|bar|area|point, headline? (the figure alone, e.g. 18 with unit km), caption? (≤120), "
    "comparison?, tone?, unit?, x:[labels, text or numbers], series:[{name, "
    "values:[one per x, number|null]}] (1-6, ≤400 values in all), reference?: {value, label?}}. document: "
    "{file, quotes?: [{label?, text, page, tone?}] (1-3), or one quote? with page?; pages?}. link: {url (https), description?, image? (file)}. gallery: {images:[{file, "
    "caption?}] (1-24)}. compose: {hero?: {type:image, file, aspect?} | {type:map, route|place: <route or "
    "place data>} | {type:chart, chart: <chart data>}, blocks (1-6): text {text ≤400, emphasis?: body|lead|"
    "caption}, stats {items: 2-3 {label, value, unit?, delta?, tone?}}, rows {items ≤8 {label, value, glyph?, "
    "tone?}}, meter {label, value 0-1, caption?, tone?}, spark {label, value?, points 2-60}, list {items ≤6 "
    "{title, detail?, trailing?, glyph?, checked?}}, timeline {items ≤6 {time, title, detail?, state?: done|"
    "next}}, quote {text ≤300, by?}, chips {items ≤4}, image {file, aspect?: wide|square|tall, caption?}}; a stats, "
    "rows or spark value and a list trailing may be a number, while delta, unit and time are text (\"+12%\", \"15:10\"). "
    "tone is neutral|good|caution. glyph is an SF Symbol name such as checkmark.circle, clock, calendar, "
    "airplane, car, mappin, creditcard, envelope, house, star."
)

SHOW_CARD_SCHEMA = {
    "name": "show_card",
    "description": _DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "v": {"type": "integer", "enum": [1], "description": "Card contract version; omit for 1."},
            "kind": {"type": "string", "enum": ["table", "flight", "route", "place", "weather", "chart",
                                                "document", "link", "gallery", "compose"]},
            "variant": {"type": "string", "enum": ["hero", "compact", "detail", "sheet"],
                        "description": "Optional. sheet is for table only."},
            "title": {"type": "string", "description": "Plain text, at most 80 characters."},
            "source": {"type": "object", "description": "Optional eyebrow: where the content came from.",
                       "properties": {"label": {"type": "string"},
                                      "origin": {"type": "string", "enum": ["ava", "tool", "external"]}},
                       "required": ["label"]},
            "note": {"type": "string", "description": "Optional. One sentence, at most 240 characters."},
            "alt": {"type": "string", "description": "Plain words, complete on their own, at most 1,200 characters. "
                                                     "Required for compose; derived for other kinds when omitted."},
            "data": {"type": "object", "description": _DATA_DESCRIPTION},
            "actions": {
                "type": "array", "maxItems": 2,
                "description": "At most two. directions/open_in_maps only on place and route; open_file on a "
                               "document or a table with data.file; open_url needs url except on a link card; "
                               "add_to_calendar needs title and start (ISO 8601 with offset).",
                "items": {"type": "object", "properties": {
                    "type": {"type": "string", "enum": ["open", "directions", "open_in_maps", "add_to_calendar",
                                                        "open_file", "open_url", "ask"]},
                    "text": {"type": "string", "description": "ask: the exact words placed in his composer."},
                    "url": {"type": "string"}, "title": {"type": "string"}, "start": {"type": "string"},
                    "end": {"type": "string"}, "location": {"type": "string"}},
                    "required": ["type"]},
            },
        },
        "required": ["kind", "title"],
    },
}


def cards_enabled() -> bool:
    """``cards.enabled`` for the profile this check runs under, and a validator to enforce it with."""
    if importlib.util.find_spec("jsonschema") is None:  # no validator: the tool stays off, never fails open
        return False
    from hermes_cli.config import cfg_get, load_config
    return cfg_get(load_config(), "cards", "enabled", default=False) is True


def _call_ids() -> tuple[str, str]:
    """(turn id, tool call id) the dispatcher bound around this call."""
    from tools.approval_context import _approval_tool_call_id, _approval_turn_id
    return _approval_turn_id.get() or "", _approval_tool_call_id.get() or ""


def _claim_turn_slot(session_id: str, turn_id: str) -> bool:
    """Count one accepted card against its turn; False once the turn already showed six."""
    if not turn_id:
        return True
    key = (session_id, turn_id)
    with _turn_lock:
        count = _turn_counts.pop(key, 0)
        accepted = count < MAX_CARDS_PER_TURN
        _turn_counts[key] = count + 1 if accepted else count
        while len(_turn_counts) > _TURNS_REMEMBERED:
            _turn_counts.popitem(last=False)
        return accepted


def _card_id(card: dict, session_id: str, tool_call_id: str) -> str:
    if tool_call_id:
        return tool_call_id
    from tools.cards.validate import card_bytes
    digest = hashlib.sha256(session_id.encode("utf-8") + b"\0" + card_bytes(card)).hexdigest()
    return f"card_{digest[:12]}"


def show_card(card: Any, *, session_id: Optional[str] = None) -> str:
    from tools.cards.alt import card_alt
    from tools.cards.validate import KIND_SHAPES, unknown_field_warnings, validate_card
    if not cards_enabled():  # a tool array pinned while cards were on still reaches here: the flag is the switch
        return json.dumps({"ok": False, "error": CARDS_OFF, "errors": [{"path": "(card)", "message": CARDS_OFF}]},
                          ensure_ascii=False)
    problems = validate_card(card)
    if problems:
        kind = card.get("kind") if isinstance(card, dict) else None
        refusal = {"ok": False, "error": f"card refused: {len(problems)} problem{'s' if len(problems) != 1 else ''}; "
                                         "fix each path and call show_card again, or drop the card; either way, "
                                         "say nothing about it to him",
                   "errors": [problem.as_dict() for problem in problems]}
        if kind in KIND_SHAPES:
            refusal["shape"] = KIND_SHAPES[kind]
        return json.dumps(refusal, ensure_ascii=False)
    turn_id, tool_call_id = _call_ids()
    if not _claim_turn_slot(session_id or "", turn_id):
        limit = f"this turn already shows {MAX_CARDS_PER_TURN} cards; say the rest in words"
        return json.dumps({"ok": False, "error": f"card refused: {limit}",
                           "errors": [{"path": "(card)", "message": limit}]}, ensure_ascii=False)
    return json.dumps({"ok": True, "card_id": _card_id(card, session_id or "", tool_call_id), "alt": card_alt(card),
                       "warnings": unknown_field_warnings(card)}, ensure_ascii=False)


registry.register(
    name="show_card",
    toolset="cards",
    schema=SHOW_CARD_SCHEMA,
    handler=lambda args, **kw: show_card(args, session_id=kw.get("session_id")),
    check_fn=cards_enabled,
    emoji="🃏",
    normalize_args=normalize_card,
)
