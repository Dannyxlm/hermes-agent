"""Card contract v1: the schema plus the rules a JSON Schema cannot say.

``card.schema.json`` is a byte copy of the release contract (``contract/cards-v1``); the app tests
the same bytes. This module refuses, it never rewrites: an accepted card is exactly the tool call's
arguments, so every reader can draw those arguments as they are.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SCHEMA_PATH = Path(__file__).with_name("card.schema.json")
KINDS = ("table", "flight", "route", "place", "weather", "chart", "document", "link", "gallery", "compose")
MAX_CARD_BYTES = 16 * 1024
MAX_DEPTH = 12
MAX_CHART_VALUES = 400
MAX_ERRORS = 8
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
_SURROGATE = re.compile("[\ud800-\udfff]")
# What the messaging gateway would turn into an attachment or a delivery mode (gateway/platforms/base.py
# ``extract_media``): a card shows, it never attaches. ``MEDIA:`` counts only where a path or quoted name
# could follow (a quote, ``~``, ``/`` or a drive letter): "Paid media: November results" and a wiki
# ``Media:Foo.ogg`` link are words, and the gateway attaches nothing for them.
_DELIVERY_DIRECTIVE = re.compile(r"MEDIA:\s*(?:[`\"'~/]|[A-Za-z]:[/\\])|\[\[(?:audio_as_voice|as_document)\]\]",
                                 re.IGNORECASE)
# C0, DEL, C1 (U+0085 NEL, U+009B CSI) and the Unicode line/paragraph separators, which break a line
# in SwiftUI text; a multi-line field keeps only \n.
_CONTROL_ONE_LINE = re.compile("[\u0000-\u001f\u007f-\u009f\u2028\u2029]")
_CONTROL_MULTILINE = re.compile("[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u2028\u2029]")
_ENVELOPE_KEYS = frozenset({"v", "kind", "variant", "title", "source", "note", "alt", "data", "actions"})
_FILE_ACTION_KINDS = frozenset({"document"})
_MAP_ACTIONS = frozenset({"directions", "open_in_maps"})

# One-line shape reminders returned with a refusal, so the repair needs no second lookup.
KIND_SHAPES = {
    "table": '{"columns":[{"title","type?":"text|number|money|percent|date|delta|status","currency?"}] (1-12), '
             '"rows":[{"cells":[one per column],"secondary?","badge?","tone?","highlight?"}] (≤60), "totals?","count?","file?"}',
    "flight": '{"legs":[{"number?","from","to","departure":"17:55","arrival","dayOffset?","duration?":"6 h 45","date?",'
              '"fromName?","toName?",'
              '"detail?","arrivalDetail?","fromLat?","fromLon?","toLat?","toLon?"}] (1-6), "connection?","connectionCode?",'
              '"airline?","total?","price?":"$1,482","priceNote?","leave?":{"time","label?","detail?"}} (times, durations '
              'and prices are text)',
    "route": '{"stops":[{"name","lat","lon","address?","detail?"}] (2-12), "mode?":"drive|walk|transit|cycle|flight", '
             '"duration?":"24 min by car","distance?":"21 km"} (duration and distance are text)',
    "place": '{"name","lat","lon","address?","detail?"}',
    "weather": '{"headline":"4°","condition?","comparison?","range?","chart?":<chart data>,'
               '"days?":[{"day","high?":"7°","low?":"2°","condition?"}]} (temperatures: text or a number)',
    "chart": '{"type?":"line|bar|area|point","headline?","caption?","comparison?","tone?","unit?","x":[labels],'
             '"series":[{"name","values":[one per x]}] (1-6, ≤400 values in all),"reference?":{"value","label?"}}',
    "document": '{"file":"/absolute/path","quotes?":[{"label?","text","page","tone?"}] (1-3) or "quote?","page?"; "pages?"}',
    "link": '{"url":"https://…","description?","image?":"/absolute/path"}',
    "gallery": '{"images":[{"file":"/absolute/path","caption?"}] (1-24)}',
    "compose": '{"hero?":{"type":"image","file"}|{"type":"map","route"|"place"}|{"type":"chart","chart"},'
               '"blocks":[text|stats|rows|meter|spark|list|timeline|quote|chips|image] (1-6)} and a top-level "alt"',
}


@dataclass(frozen=True)
class CardProblem:
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(schema())


def _path(parts: Iterable[Any]) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(card)"


def _describe(error) -> str:
    """A short, repairable message: never echo a long instance back at the model."""
    value, validator, expected = error.instance, error.validator, error.validator_value
    if validator == "required":
        missing = [name for name in expected if isinstance(value, dict) and name not in value]
        return f"needs {', '.join(repr(m) for m in missing)}"
    if validator in ("maxLength", "minLength") and isinstance(value, str):
        return f"is {len(value)} characters; {'at most' if validator == 'maxLength' else 'at least'} {expected}"
    if validator in ("maxItems", "minItems") and isinstance(value, list):
        count = f"{len(value)} item{'' if len(value) == 1 else 's'}"
        return f"has {count}; {'at most' if validator == 'maxItems' else 'at least'} {expected}"
    if validator == "enum":
        return f"{_short(value)} is not one of {', '.join(map(str, expected))}"
    if validator == "const":
        return f"must be {json.dumps(expected)}"
    if validator == "type":
        return _type_hint(value, expected)
    if validator in ("minimum", "maximum"):
        return f"{_short(value)} is {'below' if validator == 'minimum' else 'above'} {expected:g}"
    if validator == "additionalProperties":
        return "has a field actions do not take" if "actions" in error.absolute_path else "has fields this object does not take"
    if validator == "pattern":
        return _pattern_hint(error)
    if validator == "not" and isinstance(value, dict) and {"route", "place"} <= value.keys():
        return "takes route or place, not both"
    if validator == "oneOf":
        return "needs exactly one of route or place" if isinstance(value, dict) and value.get("type") == "map" \
            else "matches none of the allowed shapes"
    return error.message if len(error.message) < 200 else error.message[:200] + "…"


def _type_hint(value: Any, expected: Any) -> str:
    if expected == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return "must be text, not a number: write it as he should read it, with its unit"
    return f"must be {expected if isinstance(expected, str) else ' or '.join(expected)}, not {type(value).__name__}"


def _pattern_hint(error) -> str:
    pattern = str(error.validator_value)
    if pattern.startswith("^https://"):
        return "must be an https:// link without user info or spaces"
    if pattern.startswith("^/"):
        return "must be an absolute path on this server without '..'"
    if pattern.startswith("^[0-9]{4}"):
        return "must be ISO 8601 with an offset, e.g. 2027-01-14T17:55:00-05:00"
    if pattern.startswith("^[A-Z]{3}"):
        return "must be a three-letter currency code, e.g. CAD"
    return "contains characters that are not allowed (control characters or bidi overrides)"


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else repr(value)
    return text if len(text) <= 40 else text[:40] + "…"


def _deepest(error):
    """For oneOf/anyOf/if-then failures, the most specific sub-error is the one worth repairing."""
    from jsonschema.exceptions import best_match
    while error.context:
        # A type mismatch on an alternative branch (number|null) says least about what to fix.
        informative = [e for e in error.context if e.validator != "type"] or list(error.context)
        best = best_match(informative)
        if best is None or best is error:
            break
        error = best
    return error


def _schema_problems(card: dict) -> list[CardProblem]:
    problems = []
    seen = set()
    for error in sorted(_validator().iter_errors(card), key=lambda e: list(map(str, e.absolute_path))):
        error = _deepest(error)
        item = CardProblem(_path(error.absolute_path), _describe(error))
        if item not in seen:
            seen.add(item)
            problems.append(item)
    return problems


def _walk(value: Any, path: tuple, depth: int = 0):
    yield value, path, depth
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, (*path, key), depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, index), depth + 1)


def _too_deep(card: dict) -> Optional[CardProblem]:
    for _value, path, depth in _walk(card, ()):
        if depth > MAX_DEPTH:
            return CardProblem(_path(path[:MAX_DEPTH]), f"nests deeper than {MAX_DEPTH} levels")
    return None


def _multiline(path: tuple) -> bool:
    """The fields README rule 3 lets hold line breaks: alt, note, document.quote and quotes[].text,
    link.description, and the text of compose text and quote blocks."""
    if len(path) == 1:
        return path[0] in ("alt", "note")
    if len(path) == 2 and path[0] == "data":
        return path[1] in ("quote", "description")
    return len(path) == 4 and path[0] == "data" and path[1] in ("blocks", "quotes") and path[3] == "text"


def _text_problems(card: dict) -> list[CardProblem]:
    problems = []
    for value, path, _depth in _walk(card, ()):
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(CardProblem(_path(path), "must be a finite number"))
        elif isinstance(value, str):
            multiline = _multiline(path)
            if _SURROGATE.search(value):
                problems.append(CardProblem(_path(path), "contains an unpaired surrogate (not valid UTF-8 text)"))
            elif _DELIVERY_DIRECTIVE.search(value):
                problems.append(CardProblem(_path(path), "contains a MEDIA: or [[…]] delivery directive; a card "
                                                         "attaches nothing, so write the words another way"))
            elif _BIDI.search(value):
                problems.append(CardProblem(_path(path), "contains bidi control characters"))
            elif (_CONTROL_MULTILINE if multiline else _CONTROL_ONE_LINE).search(value):
                problems.append(CardProblem(_path(path), "contains control characters"
                                            + ("" if multiline else " (it is one line)")))
    return problems


def _semantic_problems(card: dict) -> list[CardProblem]:
    kind, data = card.get("kind"), card.get("data") if isinstance(card.get("data"), dict) else {}
    problems = [CardProblem(key, "is blank; write the words or leave it out" if key == "alt" else "is blank")
                for key in ("title", "alt") if isinstance(card.get(key), str) and not card[key].strip()]
    problems += _KIND_RULES.get(kind or "", lambda _data: [])(data)
    for path, chart in _charts(kind, data):
        problems += _chart_problems(path, chart)
    problems += _action_problems(card, kind, data)
    problems += _calendar_problems(card)
    return problems


def _calendar_problems(card: dict) -> list[CardProblem]:
    """The schema checks an add_to_calendar time's shape; this checks it is a real moment."""
    problems = []
    for index, action in enumerate(card.get("actions") if isinstance(card.get("actions"), list) else []):
        if not (isinstance(action, dict) and action.get("type") == "add_to_calendar"):
            continue
        for key in ("start", "end"):
            value = action.get(key)
            if not isinstance(value, str):
                continue
            try:
                _dt.datetime.fromisoformat(value)
            except ValueError:
                problems.append(CardProblem(f"actions[{index}].{key}", f"{_short(value)} is not a real date and time"))
    return problems


def _table_problems(data: dict) -> list[CardProblem]:
    problems = []
    columns = len(data.get("columns") or [])
    for index, row in enumerate(data.get("rows") or []):
        cells = row.get("cells") if isinstance(row, dict) else None
        if isinstance(cells, list) and len(cells) != columns:
            problems.append(CardProblem(f"data.rows[{index}].cells", f"has {len(cells)} cells; the table has {columns} columns"))
    totals = data.get("totals")
    if isinstance(totals, list) and len(totals) != columns:
        problems.append(CardProblem("data.totals", f"has {len(totals)} cells; the table has {columns} columns"))
    return problems + _percent_point_problems(data)


def _number_cell(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _percent_point_problems(data: dict) -> list[CardProblem]:
    """A percent column written in points (54.1 for 54.1%): readers draw fractions, so it would show as
    5,410%. Plainly points means one cell reads 1,000% or more, or every non-zero cell reads above 150%
    and there are two of them. One real 150% or 300% figure, or growth beside ordinary rates, still
    passes; a figure that truly is that large can be written as a text cell ("1,200%")."""
    from tools.cards.alt import _number, format_cell
    rows = [row.get("cells") for row in data.get("rows") or [] if isinstance(row, dict)]
    totals = data.get("totals")
    lines = [cells for cells in rows + [totals] if isinstance(cells, list)]
    problems = []
    for index, column in enumerate(data.get("columns") or []):
        if not (isinstance(column, dict) and column.get("type") == "percent"):
            continue
        values = [cells[index] for cells in lines
                  if index < len(cells) and _number_cell(cells[index]) and cells[index] != 0]
        sizes = [abs(value) for value in values]
        if sizes and (max(sizes) >= 10 or (len(sizes) >= 2 and min(sizes) > 1.5)):
            shown = max(values, key=abs)
            drawn = format_cell(shown, column)
            problems.append(CardProblem(
                f"data.columns[{index}]", f"is percent, and percent cells are fractions: {_number(shown, 6)} would show "
                f"as {drawn}. Write {_number(shown / 100, 6)} for {_number(shown, 6)}% (rows and totals), or if "
                f"{drawn} is right, write that cell as text (\"{drawn}\")"))
    return problems


def _flight_problems(data: dict) -> list[CardProblem]:
    """A leg's arc end is a whole coordinate or nothing: half a point draws no arc."""
    problems = []
    for index, leg in enumerate(data.get("legs") or []):
        for end in ("from", "to"):
            if isinstance(leg, dict) and ((f"{end}Lat" in leg) != (f"{end}Lon" in leg)):
                problems.append(CardProblem(f"data.legs[{index}]", f"needs both {end}Lat and {end}Lon, or neither"))
    return problems


def _document_problems(data: dict) -> list[CardProblem]:
    if "quote" in data and "quotes" in data:
        return [CardProblem("data", "takes quotes or quote, not both")]
    pages = data.get("pages")
    if not isinstance(pages, (int, float)) or isinstance(pages, bool) or pages != int(pages):
        return []  # 3.0 is an integer to the schema, so it counts here too
    cited = [("data.page", data.get("page"))] + [(f"data.quotes[{index}].page", entry.get("page"))
                                                 for index, entry in enumerate(data.get("quotes") or [])]
    return [CardProblem(path, f"is page {page:g}; the document has {pages:g}")
            for path, page in cited if isinstance(page, (int, float)) and not isinstance(page, bool) and page > pages]


_KIND_RULES: dict[str, Callable[[dict], list[CardProblem]]] = {
    "table": _table_problems,
    "flight": _flight_problems,
    "document": _document_problems,
}


def _charts(kind: Optional[str], data: dict):
    if kind == "chart":
        yield "data", data
    elif kind == "weather" and isinstance(data.get("chart"), dict):
        yield "data.chart", data["chart"]
    elif kind == "compose" and isinstance(data.get("hero"), dict) and isinstance(data["hero"].get("chart"), dict):
        yield "data.hero.chart", data["hero"]["chart"]


def _chart_problems(path: str, chart: dict) -> list[CardProblem]:
    problems = []
    x = chart.get("x") if isinstance(chart.get("x"), list) else []
    series = chart.get("series") if isinstance(chart.get("series"), list) else []
    total = 0
    for index, entry in enumerate(series):
        values = entry.get("values") if isinstance(entry, dict) else None
        if isinstance(values, list):
            total += len(values)
            if len(values) != len(x):
                problems.append(CardProblem(f"{path}.series[{index}].values", f"has {len(values)} values; x has {len(x)}"))
    if total > MAX_CHART_VALUES:
        problems.append(CardProblem(f"{path}.series", f"holds {total} values; at most {MAX_CHART_VALUES} in all"))
    return problems


def _action_problems(card: dict, kind: Optional[str], data: dict) -> list[CardProblem]:
    actions = card.get("actions") if isinstance(card.get("actions"), list) else []
    problems = []
    seen, urls = [], 0
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        path, kind_of = f"actions[{index}]", action.get("type")
        if action in seen:
            problems.append(CardProblem(path, "repeats an earlier action"))
        seen.append(action)
        if kind_of in _MAP_ACTIONS and kind not in ("place", "route"):
            problems.append(CardProblem(path, f"{kind_of} is only for place and route cards"))
        if kind_of == "open_file" and not (kind in _FILE_ACTION_KINDS or (kind == "table" and data.get("file"))):
            problems.append(CardProblem(path, "open_file needs a document card, or a table with data.file"))
        if kind_of == "open_url":
            urls += 1
            if urls > 1:
                problems.append(CardProblem(path, "at most one open_url per card"))
            if kind != "link" and not action.get("url"):
                problems.append(CardProblem(path, "open_url needs its own url outside a link card"))
    return problems


def _gallery_files(data: dict) -> list[tuple[str, Any]]:
    return [(f"data.images[{index}].file", image.get("file") if isinstance(image, dict) else None)
            for index, image in enumerate(data.get("images") or [])]


def _compose_files(data: dict) -> list[tuple[str, Any]]:
    hero = data.get("hero")
    found = [("data.hero.file", hero.get("file"))] if isinstance(hero, dict) and hero.get("type") == "image" else []
    return found + [(f"data.blocks[{index}].file", block.get("file"))
                    for index, block in enumerate(data.get("blocks") or [])
                    if isinstance(block, dict) and block.get("type") == "image"]


_FILES_BY_KIND: dict[str, Callable[[dict], list[tuple[str, Any]]]] = {
    "document": lambda data: [("data.file", data.get("file"))],
    "table": lambda data: [("data.file", data.get("file"))],
    "link": lambda data: [("data.image", data.get("image"))],
    "gallery": _gallery_files,
    "compose": _compose_files,
}


def card_files(card: dict) -> list[tuple[str, str]]:
    """Every file the card references, as ``(path in the card, file path)``, in reading order."""
    data = card.get("data") if isinstance(card.get("data"), dict) else {}
    extract = _FILES_BY_KIND.get(card.get("kind") or "")
    return [(path, value) for path, value in (extract(data) if extract else []) if isinstance(value, str)]


def server_file_problem(path: str) -> Optional[str]:
    """Why the app could not fetch *path* from this server, or None when it can.

    The same predicate gates the fetch: accepting a card grants its files (``file_grants``), and every fetch of a
    granted file re-runs this check, so a file the validator accepts is one the app can load."""
    from agent.file_safety import get_read_block_error
    from tools.cards.file_grants import secret_bearing
    from tools.file_tools import _is_blocked_device
    target = Path(path)
    if get_read_block_error(path) or _is_blocked_device(path):  # read_file's two guards, in its order
        return "is not readable through Hermes (credential, device or process path)"
    if secret_bearing(os.path.realpath(path)):
        return "has a key or credential name (.ssh, .aws, *.pem, *.key …); cards never show it"
    try:
        if not target.is_file():
            return "does not exist on this server" if not target.exists() else "is not a regular file"
    except OSError:
        return "cannot be read on this server"
    return None


def card_bytes(card: Any) -> bytes:
    """The card's compact UTF-8 JSON (what the 16 KB limit measures)."""
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_card(card: Any, *, file_problem: Callable[[str], Optional[str]] = server_file_problem) -> list[CardProblem]:
    """Every reason to refuse *card*, at most ``MAX_ERRORS``; empty means accept it exactly as given."""
    if not isinstance(card, dict):
        return [CardProblem("(card)", f"must be an object, not {type(card).__name__}")]
    if (deep := _too_deep(card)) is not None:
        return [deep]
    problems = _text_problems(card)
    try:
        size = len(card_bytes(card))
    except ValueError:
        return problems or [CardProblem("(card)", "numbers must be finite")]
    if size > MAX_CARD_BYTES:
        return [CardProblem("(card)", f"is {size:,} bytes of JSON; at most {MAX_CARD_BYTES:,}. "
                                      "Put bulk data in a file and reference it")]
    said = {problem.path for problem in problems}  # the pattern error repeats a control-character finding
    problems += [problem for problem in _schema_problems(card)
                 if not (problem.path in said and problem.message.startswith("contains characters"))]
    if not problems:
        problems += _semantic_problems(card)
    if not problems:
        for path, value in card_files(card):
            reason = file_problem(value)
            if reason:
                problems.append(CardProblem(path, f"{value} {reason}"))
    return problems[:MAX_ERRORS]


def unknown_field_warnings(card: dict) -> list[str]:
    """Accepted-but-ignored fields at the top two levels: a reader keeps them and draws nothing from
    them, which is usually a typo worth hearing about."""
    warnings = [f"ignored field {key!r}: a reader keeps it but draws nothing from it"
                for key in card if key not in _ENVELOPE_KEYS]
    kind, data = card.get("kind"), card.get("data")
    known = (schema()["$defs"].get(kind) or {}).get("properties") if isinstance(kind, str) else None
    if isinstance(data, dict) and isinstance(known, dict):
        warnings += [f"ignored field 'data.{key}': {kind} cards do not draw it" for key in data if key not in known]
    return warnings
