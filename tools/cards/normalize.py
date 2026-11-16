"""Numbers sent where a card draws text, written as the text the app draws (contract rule 14).

A model sends ``"high": 7`` as naturally as ``7`` in a table cell, but a weather day draws text, and the
app reads only text there. This runs when the call arrives, before the turn is persisted or shown
(``agent/tool_call_normalize.py``), so the app, live and in history, gets text it already draws, and
the validator, which stays strict, accepts it on the first call.

Only where the number alone says what to draw: a temperature (with the card's degree mark) and a figure,
label or count (its digits). A number that needs a unit the card cannot know (a duration, a distance, a
price, a time) is left as sent, and the refusal asks for text.
"""
from __future__ import annotations

import copy
import math
import re
from typing import Any, Callable

_DEGREE_MARKS = ("°", "°C", "°F")
_STATED_MARK = re.compile(r"(°[CF]?)\s*$")


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isinstance(value, int) or math.isfinite(value)  # NaN and infinities stay for the validator


def figure(value: float) -> str:
    """A number as its digits: 7 → "7", 7.0 → "7", 2.5 → "2.5", 2024 → "2024" (never grouped: years)."""
    if isinstance(value, int):
        return str(value)
    return str(int(value)) if value.is_integer() else format(value, ".15g")


def _write(holder: Any, key: Any, text: Callable[[Any], str]) -> bool:
    if isinstance(holder, dict) and key in holder and _is_number(holder[key]):
        holder[key] = text(holder[key])
        return True
    if isinstance(holder, list) and isinstance(key, int) and _is_number(holder[key]):
        holder[key] = text(holder[key])
        return True
    return False


def _dicts(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _degree_mark(data: dict) -> str:
    """The card's stated mark: the chart's degree unit, else the headline's (``4°C``), else ``°``."""
    chart = data.get("chart")
    unit = chart.get("unit") if isinstance(chart, dict) else None
    if unit in _DEGREE_MARKS:
        return unit
    headline = data.get("headline")
    stated = _STATED_MARK.search(headline) if isinstance(headline, str) else None
    return stated.group(1) if stated else "°"


def _chart(chart: Any) -> bool:
    if not isinstance(chart, dict):
        return False
    changed = _write(chart, "headline", figure)
    x = chart.get("x")
    for index in range(len(x) if isinstance(x, list) else 0):
        changed |= _write(x, index, figure)
    return changed


def _weather(data: dict) -> bool:
    mark = _degree_mark(data)

    def temperature(value: float) -> str:
        return figure(round(value, 1) if isinstance(value, float) else value) + mark

    changed = _write(data, "headline", temperature)
    for day in _dicts(data.get("days")):
        changed |= _write(day, "high", temperature)
        changed |= _write(day, "low", temperature)
    return _chart(data.get("chart")) | changed


# compose block type -> (list key or None, field): the text fields a figure or count naturally fills.
_BLOCK_FIGURES = {"stats": ("items", "value"), "rows": ("items", "value"), "list": ("items", "trailing"),
                  "spark": (None, "value")}


def _compose(data: dict) -> bool:
    hero = data.get("hero")
    changed = _chart(hero.get("chart")) if isinstance(hero, dict) and hero.get("type") == "chart" else False
    for block in _dicts(data.get("blocks")):
        kind = block.get("type")
        if not (isinstance(kind, str) and kind in _BLOCK_FIGURES):
            continue
        items_key, field = _BLOCK_FIGURES[kind]
        for holder in (_dicts(block.get(items_key)) if items_key else [block]):
            changed |= _write(holder, field, figure)
    return changed


_WRITERS: dict[str, Callable[[dict], bool]] = {"weather": _weather, "chart": _chart, "compose": _compose}


def normalize_card(card: Any) -> Any:
    """*card* with those numbers written as text, or *card* itself (the same object) when none are."""
    kind = card.get("kind") if isinstance(card, dict) else None
    writer = _WRITERS.get(kind) if isinstance(kind, str) else None
    if writer is None or not isinstance(card.get("data"), dict):
        return card
    data = copy.deepcopy(card["data"])
    return {**card, "data": data} if writer(data) else card
