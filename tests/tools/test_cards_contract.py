"""Card contract v1: the shared bytes, every fixture accepted or refused as the manifest says, and the
``alt`` words for every valid card.

The fixtures are a byte copy of the release contract (``contract/cards-v1``) that the app also tests;
``SHA256SUMS`` proves both sides read the same bytes.
"""
import hashlib
import json
from pathlib import Path

import pytest

from tools.cards.alt import card_alt, format_cell
from tools.cards.validate import SCHEMA_PATH, validate_card

CONTRACT = Path(__file__).parent / "fixtures" / "cards_v1"
MANIFEST = json.loads((CONTRACT / "fixtures.json").read_text(encoding="utf-8"))["fixtures"]
SYNTHETIC = "/synthetic/cards/"


def _synthetic_files(path: str):
    """Fixture files live under the synthetic app profile; the real file check has its own tests."""
    return None if path.startswith(SYNTHETIC) else "does not exist on this server"


def _load(entry):
    text = (CONTRACT / entry["file"]).read_text(encoding="utf-8")
    return json.loads(text)  # Python's json reads NaN; the validator must refuse it


def test_contract_bytes_match_their_checksums():
    lines = (CONTRACT / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    listed = {}
    for line in lines:
        digest, name = line.split(maxsplit=1)
        listed[name] = digest
    on_disk = {p.relative_to(CONTRACT).as_posix() for p in CONTRACT.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    assert set(listed) == on_disk
    for name, digest in listed.items():
        assert hashlib.sha256((CONTRACT / name).read_bytes()).hexdigest() == digest, name


def test_the_validator_reads_the_contract_schema():
    assert SCHEMA_PATH.read_bytes() == (CONTRACT / "card.schema.json").read_bytes()


def test_manifest_lists_every_fixture_once_and_covers_every_kind():
    files = [entry["file"] for entry in MANIFEST]
    assert len(files) == len(set(files))
    assert set(files) == {p.relative_to(CONTRACT).as_posix() for p in (CONTRACT / "fixtures").rglob("*.json")}
    kinds = {entry["kind"] for entry in MANIFEST if entry["expect"] == "accept"}
    assert kinds == {"table", "flight", "route", "place", "weather", "chart", "document", "link", "gallery", "compose"}


@pytest.mark.parametrize("entry", [e for e in MANIFEST if e["expect"] == "accept"], ids=lambda e: e["file"])
def test_valid_fixture_is_accepted(entry):
    assert validate_card(_load(entry), file_problem=_synthetic_files) == []


@pytest.mark.parametrize("entry", [e for e in MANIFEST if e["expect"] == "reject"], ids=lambda e: e["file"])
def test_hostile_and_forward_fixtures_are_refused_with_a_path(entry):
    problems = validate_card(_load(entry), file_problem=_synthetic_files)
    assert problems, entry["about"]
    assert all(problem.path and problem.message for problem in problems)
    assert len(json.dumps([p.as_dict() for p in problems])) < 4000  # never echoes a huge payload back


ALT = {
    "table-hero": "Three ways to Kigali\nAirline · Time · Fare\nKLM · 18 h 55 · $1,482 (via Amsterdam) — Ava’s pick\n"
                  "Qatar · 22 h 10 · $1,395 (via Doha)\nBrussels · 19 h 40 · $1,610 (via Brussels)",
    "table-sheet": "Card balances\nCard · Balance · Due\nAmex Cobalt · $2,184.20 · Nov 28\nTD Visa Infinite · $640.12 · Dec 4\n"
                   "Tangerine Mastercard · $588 · Dec 11\nOwed · 3 cards · $3,412.32 · —",
    "table-minimal": "Two numbers\nItem · Count\nOpen findings · 57\nShown today · 3",
    "flight-hero": "Montréal to Kigali\nKL 672 YUL 17:55 → AMS 06:40 (+1), Thu Jan 14, 6 h 45\n"
                   "KL 535 AMS 10:25 → KGL 19:50, Fri Jan 15, 8 h 25\nConnection: 3 h 45 in Amsterdam · same terminal",
    "flight-single": "Back to Montréal\nAC 829 KGL 21:10 → YUL 07:35 (+1)",
    "route-hero": "Airport to the hotel: YUL · Montréal airport → Hôtel William Gray, 24 min by car, 21 km.\n"
                  "https://maps.apple.com/?saddr=45.4577,-73.7499&daddr=45.5075,-73.554&dirflg=d",
    "route-flight": "Montréal, Amsterdam, Kigali: YUL → AMS → KGL, 21 h 45 door to door.",
    "place-hero": "Café Olimpico\n124 Rue Saint-Viateur O, Montréal\nOpen until 23:00\n"
                  "https://maps.apple.com/?ll=45.5228,-73.6003&q=Caf%C3%A9%20Olimpico",
    "place-minimal": "Meeting point: Place des Arts\nhttps://maps.apple.com/?ll=45.5085,-73.5668&q=Place%20des%20Arts",
    "weather-hero": "Montréal · Saturday: 4°, cloudy. Rain from 15:00. High 7° · low 2°.\n"
                    "Sat 7°/2° rain · Sun 5°/-1° snow · Mon 3°/-4° clear",
    "weather-compact": "Kigali now: 24°, partly cloudy.",
    "chart-bar": "Card spending: $3,412 — 11% over your average.\nSpent: 2,890 (Jun) → 3,412 (Nov)\nAverage: 3,087",
    "chart-line": "Orders by week: 142 — Up 9% on last month.\nThis year: 120 (W1) → 142 (W5)\nLast year: 101 (W1) → 119 (W5)",
    "document-hero": "Lease renewal – 4127 rue Clark · 3 pages\n"
                     "“The monthly rent will increase from $2,150 to $2,236 beginning July 1, 2027.” (page 2)",
    "document-minimal": "Q3 report",
    "link-hero": "Loft near the Lachine Canal\nEntire loft · 2 guests · self check-in\n"
                 "https://www.airbnb.com/rooms/000000?check_in=2027-01-14",
    "link-minimal": "Passport renewal\nhttps://www.canada.ca/en/immigration-refugees-citizenship/services/canadian-passports.html",
    "gallery-set": "Saturday on the mountain: 12 images.",
    "gallery-single": "The new logo: 1 image.\nFinal, on paper",
    "compose-trip": "Leave home Thu 14:30; YUL 17:55 to AMS 06:40 Friday. Connect 3 h 45. AMS 10:25 to KGL 19:50. "
                    "Door to door 21 h 45.",
    "compose-blocks": "Revenue $18.4k (+12%). 3 of 5 launch tasks done. Next: send the deck Thursday.",
    "compose-image": "The redesigned landing page: shorter headline, one button.",
    "compose-place": "Dinner Friday 19:30 at Joe Beef, 2491 Rue Notre-Dame O. A table for four.",
    "open-extra-fields": "Open fields stay raw: YUL · Montréal airport → Hôtel William Gray, 24 min by car, 21 km.\n"
                         "https://maps.apple.com/?saddr=45.4577,-73.7499&daddr=45.5075,-73.554&dirflg=d",
    "flight-stage": "Montréal to Kigali\nKLM · 18 h 55 · $1,482 (ends Friday)\nLeave home 15:10: 25 min to YUL by taxi\n"
                    "KL 672 Montréal (YUL) 17:55 → Amsterdam (AMS) 06:40 (+1), Thu Jan 14, 6 h 45, seat 23A, window\n"
                    "KL 535 Amsterdam (AMS) 10:25 → Kigali (KGL) 19:50, Fri Jan 15, 8 h 25, seat 31K, window\n"
                    "Kigali: Visa on arrival. 24° and dry.\nConnection: 3 h 45 in Amsterdam · same terminal",
    "document-quotes": "Lease renewal, 4127 Saint-Denis · 12 pages\nRent: $2,236 a month (page 2)\n"
                       "Term: Jul 1, 2027 to Jun 30, 2028 (page 1)\nSay no by: March 31 (page 7)",
    "route-stop-detail": "Walk to Olimpico: Home → Café Olimpico, 31 min walk, 2.2 km.\n"
                         "Café Olimpico: 124 Rue Saint-Viateur O · open till midnight\n"
                         "https://maps.apple.com/?saddr=45.5191,-73.5836&daddr=45.5228,-73.6003&dirflg=w",
    "chart-caption": "Card spending: $3,412 this month — 11% over your average.\nSpent: 2,890 (Jun) → 3,412 (Nov)",
}

# Contract rev 3: each new field's refusal names the field to repair, once.
REV3_REFUSALS = {
    "document-four-quotes": ("data.quotes", "has 4 items; at most 3"),
    "document-quote-no-page": ("data.quotes[0]", "needs 'page'"),
    "document-quote-and-quotes": ("data", "takes quotes or quote, not both"),
    "document-quote-past-last-page": ("data.quotes[2].page", "is page 14; the document has 12"),
    "flight-half-coordinate": ("data.legs[0]", "needs both toLat and toLon, or neither"),
    "flight-city-too-long": ("data.legs[1].toName", "is 45 characters; at most 40"),
    "flight-leave-no-time": ("data.leave", "needs 'time'"),
    "chart-caption-too-long": ("data.caption", "is 124 characters; at most 120"),
    "route-stop-detail-two-lines": ("data.stops[1].detail", "contains control characters (it is one line)"),
}


@pytest.mark.parametrize("name", sorted(REV3_REFUSALS))
def test_rev3_refusals_name_the_field(name):
    problems = validate_card(_load({"file": f"fixtures/hostile/{name}.json"}), file_problem=_synthetic_files)
    assert [(p.path, p.message) for p in problems] == [REV3_REFUSALS[name]]


# Contract rev 4 (review fixes): each refusal names its field and why, once.
REV4_REFUSALS = {
    "title-nel": ("title", "contains control characters (it is one line)"),
    "cell-csi": ("data.rows[0].cells[0]", "contains control characters (it is one line)"),
    "title-line-separator": ("title", "contains control characters (it is one line)"),
    "note-paragraph-separator": ("note", "contains control characters"),
    "cell-media-directive": ("data.rows[0].secondary", "contains a MEDIA: or [[…]] delivery directive; a card attaches "
                                                       "nothing, so write the words another way"),
    "note-as-document-directive": ("note", "contains a MEDIA: or [[…]] delivery directive; a card attaches nothing, "
                                           "so write the words another way"),
    "alt-blank": ("alt", "is blank; write the words or leave it out"),
    "title-blank": ("title", "is blank"),
    "calendar-not-a-date": ("actions[1].start", "'2027-13-45T99:99Z' is not a real date and time"),
    "calendar-arabic-digits": ("actions[1].start", "must be ISO 8601 with an offset, e.g. 2027-01-14T17:55:00-05:00"),
    "document-page-past-float-pages": ("data.quotes[2].page", "is page 7; the document has 3"),
}


@pytest.mark.parametrize("name", sorted(REV4_REFUSALS))
def test_rev4_refusals_name_the_field(name):
    problems = validate_card(_load({"file": f"fixtures/hostile/{name}.json"}), file_problem=_synthetic_files)
    assert [(p.path, p.message) for p in problems] == [REV4_REFUSALS[name]]


def test_the_process_file_fixture_is_refused_by_the_real_file_check():
    from tools.cards.validate import server_file_problem
    problems = validate_card(_load({"file": "fixtures/hostile/document-proc-environ.json"}))
    assert [(p.path, p.message) for p in problems] == [
        ("data.file", "/proc/self/environ is not readable through Hermes (credential, device or process path)")]
    assert server_file_problem("/proc/self/environ") is not None


def test_no_valid_fixture_claims_an_effect():
    """A card shows; nothing in a valid card says a thing was held, booked, paid, shared or signed in."""
    import re
    claims = re.compile(r"\b(held|hold|booked|paid|shared|signed in)\b", re.IGNORECASE)
    for entry in (e for e in MANIFEST if e["expect"] == "accept"):
        card = _load(entry)
        assert not claims.search(json.dumps(card, ensure_ascii=False)), entry["file"]
        assert not claims.search(card_alt(card)), entry["file"]


def test_rev3_fields_are_drawn_not_warned_about():
    from tools.cards.validate import unknown_field_warnings
    for name in ("flight-stage", "document-quotes", "route-stop-detail", "chart-caption"):
        assert unknown_field_warnings(_load({"file": f"fixtures/valid/{name}.json"})) == [], name


@pytest.mark.parametrize("entry", [e for e in MANIFEST if e["expect"] == "accept"], ids=lambda e: e["file"])
def test_alt_snapshot(entry):
    name = Path(entry["file"]).stem
    alt = card_alt(_load(entry))
    assert alt == ALT[name]
    assert len(alt) <= 1200 and "{" not in alt and "MEDIA:" not in alt


def test_alt_is_bounded_at_a_line_for_a_full_table():
    rows = [{"cells": [f"Row {i} " + "x" * 40, i * 1000.5]} for i in range(60)]
    card = {"kind": "table", "title": "Sixty rows", "data": {"columns": [{"title": "Name"}, {"title": "Amount", "type": "money"}],
                                                              "rows": rows, "count": 200}}
    assert validate_card(card) == []
    alt = card_alt(card)
    assert len(alt) <= 1200 and alt.endswith("\n…")
    assert alt.splitlines()[2].startswith("Row 0 ")


@pytest.mark.parametrize(("value", "column", "words"), [
    (2184.2, {"type": "money"}, "$2,184.20"), (-12, {"type": "money", "currency": "EUR"}, "-€12"),
    (1500, {"type": "money", "currency": "CHF"}, "1,500 CHF"), (0.745, {"type": "percent"}, "74.5%"),
    (3, {"type": "delta"}, "+3"), ("2026-11-28", {"type": "date"}, "Nov 28"), (None, {}, "—"),
    ("$1,482", {"type": "money"}, "$1,482"),
])
def test_cells_read_as_words(value, column, words):
    assert format_cell(value, column) == words
