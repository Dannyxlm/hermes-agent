"""Plain words for a card: what every surface that cannot draw it shows instead.

Derived only from an accepted card, so every field is already typed and bounded. The words carry
links (Apple Maps, the page) but never a ``MEDIA:`` directive (``defang``): a reader that falls back to
``alt`` draws it as text, and a card's files stay on the server (the messaging fallback sends words only).
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Callable, Optional
from urllib.parse import quote

MAX_ALT = 1200
# The validator refuses these in any field, but derived words join fields ("MEDIA" + ": " + a stop name),
# so the words are defanged once more on the way out: a colon after MEDIA never stays touching it.
_DIRECTIVE = re.compile(r"(MEDIA)(:)|\[\[(audio_as_voice|as_document)\]\]", re.IGNORECASE)


def defang(text: str) -> str:
    """*text* with no ``MEDIA:`` attachment or ``[[…]]`` delivery directive left for a gateway to obey."""
    return _DIRECTIVE.sub(lambda m: f"{m.group(1)} :" if m.group(1) else f"[{m.group(3)}]", text)
_CURRENCY_SIGNS = {"CAD": "$", "USD": "US$", "EUR": "€", "GBP": "£", "AUD": "A$", "JPY": "¥", "RWF": "RF "}
_MAPS_FLAG = {"drive": "d", "walk": "w", "transit": "r"}
_CONDITION_WORDS = {"partly_cloudy": "partly cloudy"}


def _number(value: float, decimals: int = 2) -> str:
    text = f"{value:,.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_cell(value: Any, column: dict) -> str:
    """One table cell as words, by column type. Strings are shown as written."""
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _date(value) if column.get("type") == "date" else str(value)
    kind = column.get("type") or "text"
    if kind == "money":
        currency = column.get("currency") or "CAD"
        amount = f"{abs(value):,.0f}" if float(value).is_integer() else f"{abs(value):,.2f}"
        sign = "-" if value < 0 else ""
        prefix = _CURRENCY_SIGNS.get(currency)
        return f"{sign}{prefix}{amount}" if prefix else f"{sign}{amount} {currency}"
    if kind == "percent":
        return f"{_number(value * 100, 1)}%"
    if kind == "delta":
        return ("+" if value > 0 else "") + _number(value)
    return _number(value)


def _date(value: Any) -> str:
    try:
        day = _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{day:%b} {day.day}"


def _lines(title: str, body: list[Optional[str]]) -> str:
    return "\n".join([title, *[line for line in body if line]])


def _table(card: dict) -> str:
    data = card["data"]
    columns = data["columns"]
    rows = data.get("rows") or []

    def row_text(cells: list, row: dict) -> str:
        text = " · ".join(format_cell(cell, col) for cell, col in zip(cells, columns))
        if row.get("secondary"):
            text += f" ({row['secondary']})"
        if row.get("badge"):
            text += f" — {row['badge']}"
        return text

    body = [" · ".join(col["title"] for col in columns)]
    body += [row_text(row.get("cells") or [], row) for row in rows]
    if data.get("totals"):
        body.append(" · ".join(format_cell(cell, col) for cell, col in zip(data["totals"], columns)))
    hidden = max(int(data.get("count") or 0), len(rows)) - len(rows)
    if hidden:
        body.append(f"+{hidden} more rows")
    return _lines(card["title"], body)


def _airport(code: str, name: Optional[str]) -> str:
    return f"{name} ({code})" if name else code


def _flight_summary(data: dict) -> Optional[str]:
    price = data.get("price")
    if price and data.get("priceNote"):
        price += f" ({data['priceNote']})"
    return " · ".join(part for part in (data.get("airline"), data.get("total"), price) if part) or None


def _flight_leave(data: dict) -> Optional[str]:
    leave = data.get("leave")
    if not leave:
        return None
    return f"{leave.get('label') or 'Leave'} {leave['time']}" + (f": {leave['detail']}" if leave.get("detail") else "")


def _flight(card: dict) -> str:
    data = card["data"]
    body = [_flight_summary(data), _flight_leave(data)]
    for leg in data["legs"]:
        day = leg.get("dayOffset") or 0
        arrival = leg["arrival"] + (f" (+{day})" if day > 0 else f" ({day})" if day < 0 else "")
        route = f"{_airport(leg['from'], leg.get('fromName'))} {leg['departure']} → {_airport(leg['to'], leg.get('toName'))} {arrival}"
        line = " ".join(part for part in (leg.get("number"), route) if part)
        extras = [part for part in (leg.get("date"), leg.get("duration"), leg.get("detail")) if part]
        body.append(line + (f", {', '.join(extras)}" if extras else ""))
        if leg.get("arrivalDetail"):
            body.append(f"{leg.get('toName') or leg['to']}: {leg['arrivalDetail']}")
    if data.get("connection"):
        body.append(f"Connection: {data['connection']}")
    return _lines(card["title"], body)


def _maps_route(stops: list[dict], mode: str) -> Optional[str]:
    if mode == "flight":
        return None
    start, end = stops[0], stops[-1]
    url = f"https://maps.apple.com/?saddr={start['lat']},{start['lon']}&daddr={end['lat']},{end['lon']}"
    return url + (f"&dirflg={_MAPS_FLAG[mode]}" if mode in _MAPS_FLAG else "")


def _route(card: dict) -> str:
    data = card["data"]
    mode = data.get("mode") or "drive"
    line = " → ".join(stop["name"] for stop in data["stops"])
    facts = [fact for fact in (data.get("duration"), data.get("distance")) if fact]
    about = [f"{stop['name']}: " + " · ".join(part for part in (stop.get("address"), stop.get("detail")) if part)
             for stop in data["stops"] if stop.get("address") or stop.get("detail")]
    return _lines(f"{card['title']}: {line}" + (f", {', '.join(facts)}" if facts else "") + ".",
                  [*about, _maps_route(data["stops"], mode)])


def _maps_place(place: dict) -> str:
    return f"https://maps.apple.com/?ll={place['lat']},{place['lon']}&q={quote(place['name'])}"


def _place(card: dict) -> str:
    data = card["data"]
    head = card["title"] if data["name"] == card["title"] else f"{card['title']}: {data['name']}"
    return _lines(head, [data.get("address"), data.get("detail"), _maps_place(data)])


def _weather(card: dict) -> str:
    data = card["data"]
    now = data["headline"]
    if data.get("condition"):
        condition = str(data["condition"])
        now += ", " + _CONDITION_WORDS.get(condition, condition)
    sentence = ". ".join(part for part in (now, data.get("comparison"), data.get("range")) if part)
    days = []
    for day in data.get("days") or []:
        temps = "/".join(t for t in (day.get("high"), day.get("low")) if t)
        cond = _CONDITION_WORDS.get(day.get("condition") or "", day.get("condition") or "")
        days.append(" ".join(part for part in (day["day"], temps, cond) if part))
    return _lines(f"{card['title']}: {sentence}.", [" · ".join(days)])


def _chart_summary(chart: dict) -> list[str]:
    x, unit = chart["x"], chart.get("unit") or ""
    lines = []
    for series in chart["series"]:
        points = [(label, value) for label, value in zip(x, series["values"]) if value is not None]
        if not points:
            continue
        (first_x, first), (last_x, last) = points[0], points[-1]
        span = f"{_number(first)}{unit} ({first_x}) → {_number(last)}{unit} ({last_x})" if len(points) > 1 \
            else f"{_number(last)}{unit} ({last_x})"
        lines.append(f"{series['name']}: {span}")
    reference = chart.get("reference")
    if reference:
        lines.append(f"{reference.get('label') or 'Reference'}: {_number(reference['value'])}{unit}")
    return lines


def _chart(card: dict) -> str:
    data = card["data"]
    head = card["title"]
    headline = " ".join(part for part in (data.get("headline"), data.get("caption")) if part)
    lead = " — ".join(part for part in (headline, data.get("comparison")) if part)
    return _lines(f"{head}: {lead}." if lead else head, _chart_summary(data))


def _document(card: dict) -> str:
    data = card["data"]
    pages = data.get("pages")
    head = card["title"] + (f" · {pages} page{'s' if pages != 1 else ''}" if pages else "")
    quotes = data.get("quotes") or ([{"text": data["quote"], "page": data.get("page")}] if data.get("quote") else [])
    lines = [(f"{entry['label']}: {entry['text']}" if entry.get("label") else f"“{entry['text']}”")
             + (f" (page {entry['page']})" if entry.get("page") else "") for entry in quotes]
    return _lines(head, lines)


def _link(card: dict) -> str:
    data = card["data"]
    return _lines(card["title"], [data.get("description"), data["url"]])


def _gallery(card: dict) -> str:
    images = card["data"]["images"]
    count = len(images)
    captions = [image["caption"] for image in images if image.get("caption")]
    head = f"{card['title']}: {count} image{'s' if count != 1 else ''}."
    return _lines(head, ["; ".join(captions)])


_BUILDERS: dict[str, Callable[[dict], str]] = {
    "table": _table, "flight": _flight, "route": _route, "place": _place, "weather": _weather,
    "chart": _chart, "document": _document, "link": _link, "gallery": _gallery,
}


def _bounded(text: str) -> str:
    """At most MAX_ALT characters, cut at a line where possible."""
    if len(text) <= MAX_ALT:
        return text
    lines, out = text.split("\n"), []
    for line in lines:
        if len("\n".join([*out, line])) > MAX_ALT - 2:
            break
        out.append(line)
    return "\n".join(out) + "\n…" if out else text[: MAX_ALT - 1] + "…"


def card_alt(card: dict) -> str:
    """The card's own ``alt`` when it has one, else words derived from its data (compose always has
    its own: the schema requires it)."""
    if isinstance(card.get("alt"), str) and card["alt"].strip():
        return defang(card["alt"])
    builder = _BUILDERS.get(card.get("kind") or "")
    return defang(_bounded(builder(card)) if builder else card.get("title", ""))
