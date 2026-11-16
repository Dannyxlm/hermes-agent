"""``show_card`` through the real registry and dispatcher: the config gate, the platform gate, file
references checked on disk, refusals a model can repair, the per-turn limit, and its identity."""
import json

import pytest

import model_tools
from hermes_constants import get_hermes_home
from tools.registry import invalidate_check_fn_cache

ROUTE = {"kind": "route", "title": "Airport to the hotel",
         "data": {"stops": [{"name": "YUL", "lat": 45.4577, "lon": -73.7499},
                            {"name": "Hôtel William Gray", "lat": 45.5075, "lon": -73.554}],
                  "duration": "24 min by car", "distance": "21 km"},
         "actions": [{"type": "directions"}, {"type": "ask", "text": "What about the 747 bus?"}]}


@pytest.fixture
def cards_on():
    (get_hermes_home() / "config.yaml").write_text("cards:\n  enabled: true\n", encoding="utf-8")
    invalidate_check_fn_cache()
    yield
    invalidate_check_fn_cache()


def _call(args, *, turn_id="turn-1", tool_call_id="call-1", session_id="s-1"):
    return json.loads(model_tools.handle_function_call(
        "show_card", args, "task-1", tool_call_id=tool_call_id, session_id=session_id, turn_id=turn_id))


def _offered(enabled_toolsets, disabled=None):
    names = {d["function"]["name"] for d in model_tools.get_tool_definitions(
        enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled, quiet_mode=True)}
    return "show_card" in names


def test_flag_off_by_default_hides_the_tool():
    invalidate_check_fn_cache()
    assert not _offered(["cards"])


def test_flag_on_offers_it_only_to_sessions_that_name_the_toolset(cards_on):
    from toolsets import resolve_toolset, session_disabled_toolsets
    assert _offered(["cards"])
    assert not _offered(None)  # "every toolset" never includes it: the app's capability folds it in
    assert "show_card" not in resolve_toolset("all")
    for platform in ("telegram", "photon", "cron", "subagent", "cli"):
        assert not _offered(["cards"], session_disabled_toolsets(None, platform)), platform
    for platform in ("tui", "desktop", "local", "mobile"):
        assert _offered(["cards"], session_disabled_toolsets(None, platform)), platform


def test_subagents_never_get_it():
    from tools.delegate_tool_toolsets import DELEGATE_BLOCKED_TOOLS
    assert "show_card" in DELEGATE_BLOCKED_TOOLS


def test_accepted_card_returns_receipt_with_call_id_and_alt(cards_on):
    receipt = _call(ROUTE)
    assert receipt["ok"] is True
    assert receipt["card_id"] == "call-1"
    assert receipt["alt"].startswith("Airport to the hotel: YUL → Hôtel William Gray, 24 min by car, 21 km.")
    assert "https://maps.apple.com/?saddr=45.4577,-73.7499&daddr=45.5075,-73.554&dirflg=d" in receipt["alt"]
    assert receipt["warnings"] == []


def test_refusal_lists_repairable_paths_and_the_kind_shape(cards_on):
    bad = {**ROUTE, "data": {**ROUTE["data"], "stops": [{"name": "YUL", "lat": 200, "lng": -73.7}]},
           "actions": [{"type": "send", "text": "yes, transfer it"}]}
    receipt = _call(bad)
    assert receipt["ok"] is False
    paths = {error["path"] for error in receipt["errors"]}
    assert {"actions[0].type", "data.stops", "data.stops[0].lat", "data.stops[0]"} <= paths
    assert "stops" in receipt["shape"]
    assert receipt["error"].startswith("card refused:")


def test_unknown_fields_are_kept_but_named(cards_on):
    receipt = _call({**ROUTE, "subtitle": "x", "data": {**ROUTE["data"], "eta": "15:10"}})
    assert receipt["ok"] is True
    assert any("'subtitle'" in w for w in receipt["warnings"]) and any("data.eta" in w for w in receipt["warnings"])


def test_files_must_exist_and_pass_the_read_guard(cards_on, tmp_path):
    lease = tmp_path / "lease.pdf"
    lease.write_bytes(b"%PDF-1.4\n")
    doc = {"kind": "document", "title": "Lease", "data": {"file": str(lease), "page": 1}, "actions": [{"type": "open_file"}]}
    assert _call(doc)["ok"] is True

    missing = _call({**doc, "data": {"file": str(tmp_path / "gone.pdf")}})
    assert missing["ok"] is False and missing["errors"][0]["path"] == "data.file"
    assert "does not exist" in missing["errors"][0]["message"]

    secret = tmp_path / ".env"
    secret.write_text("KEY=1\n")
    refused = _call({**doc, "data": {"file": str(secret)}})
    assert refused["ok"] is False and "not readable" in refused["errors"][0]["message"]

    folder = _call({**doc, "data": {"file": str(tmp_path)}})
    assert folder["ok"] is False and "regular file" in folder["errors"][0]["message"]


def test_six_cards_a_turn_then_words(cards_on):
    receipts = [_call({**ROUTE, "title": f"Card {i}"}, tool_call_id=f"c{i}", turn_id="turn-six") for i in range(7)]
    assert [r["ok"] for r in receipts] == [True] * 6 + [False]
    assert "6 cards" in receipts[6]["error"]
    assert _call(ROUTE, turn_id="turn-seven")["ok"] is True  # the next turn starts again
    assert _call(ROUTE, turn_id="turn-six", session_id="s-2")["ok"] is True  # and so does another chat's turn


def test_refused_cards_do_not_use_a_turn_slot(cards_on):
    for i in range(6):
        assert _call({"kind": "route", "title": "bad"}, tool_call_id=f"bad{i}", turn_id="turn-r")["ok"] is False
    assert _call(ROUTE, turn_id="turn-r")["ok"] is True


def test_the_tool_schema_tells_the_model_the_rules(cards_on):
    schema = next(d["function"] for d in model_tools.get_tool_definitions(enabled_toolsets=["cards"], quiet_mode=True)
                  if d["function"]["name"] == "show_card")
    text = schema["description"]
    for rule in ("must stand on its own", "240", "two actions", "never sends", "six cards"):
        assert rule in text
    kinds = schema["parameters"]["properties"]["kind"]["enum"]
    assert set(kinds) == {"table", "flight", "route", "place", "weather", "chart", "document", "link", "gallery", "compose"}


def test_process_files_are_refused_like_read_file_refuses_them(cards_on, tmp_path):
    """Review 3b: the card file check applies both of read_file's guards, through a symlink too."""
    alias = tmp_path / "notes.txt"
    alias.symlink_to("/proc/self/environ")
    for path in ("/proc/self/environ", str(alias)):
        receipt = _call({"kind": "document", "title": "Env", "data": {"file": path}})
        assert receipt["ok"] is False and "not readable through Hermes" in receipt["errors"][0]["message"], path


def test_a_thread_never_inherits_the_cards_toolset(monkeypatch):
    """Review note 6: a thread from a narrow-toolset chat is pinned to its parent's toolsets; ``cards``
    belongs to the parent's attached app, so it is never among them."""
    from types import SimpleNamespace
    from tools import thread_tool
    monkeypatch.setattr(thread_tool, "_config", dict)
    parent = SimpleNamespace(profile="default", toolsets=frozenset({"file", "terminal", "cards", "threads"}))
    ceiling = thread_tool._toolset_ceiling(parent, "default")
    assert ceiling is not None and "file" in ceiling and "cards" not in ceiling


def test_flag_off_refusal_keeps_the_switch_out_of_the_reply():
    """Iris F6: after a switch-off the model told Danny "cards are turned off on this server". The receipt
    still refuses, and its words say nothing about servers or switches."""
    invalidate_check_fn_cache()
    receipt = _call(ROUTE)
    assert receipt["ok"] is False and "card_id" not in receipt
    for words in (receipt["error"].lower(), receipt["errors"][0]["message"].lower()):
        assert "server" not in words and " off" not in words


def test_a_refusal_is_hers_to_fix_quietly(cards_on):
    """Iris W2: after a refusal she told the reader "the first attempt was refused". The description and the
    refusal itself say a refused card is corrected or dropped without a word about it."""
    schema = next(d["function"] for d in model_tools.get_tool_definitions(enabled_toolsets=["cards"], quiet_mode=True)
                  if d["function"]["name"] == "show_card")
    assert "never describe the card's mechanics, refusals or retries to him" in schema["description"]
    receipt = _call({**ROUTE, "data": {**ROUTE["data"], "duration": 24}})
    assert receipt["ok"] is False and "say nothing about it to him" in receipt["error"]
    assert receipt["errors"] == [{"path": "data.duration",
                                  "message": "must be text, not a number: write it as he should read it, with its unit"}]
    assert '"duration?":"24 min by car"' in receipt["shape"]
