"""Paper cards on the app's socket: the capability round trip, the toolset an agent is built with, and
a card's live delivery with tool progress off (events, the reconnect snapshot, the contract models)."""
import json
from types import SimpleNamespace

import pytest

from hermes_constants import get_hermes_home
import model_tools
from tools.registry import invalidate_check_fn_cache
from tui_gateway import cards_capability, server
from tui_gateway.session_stream_cut import project_event, stream_cut
from tui_gateway.transport import FanoutTransport

KINDS = ["table", "flight", "route", "place", "weather", "chart", "document", "link", "gallery", "compose"]
# A full 60-row sheet: far past the 4 KB args_text tail a reconnect snapshot keeps for other tools.
SHEET = {"kind": "table", "variant": "sheet", "title": "Sixty rows",
         "data": {"columns": [{"title": "Name"}, {"title": "Amount", "type": "money"}],
                  "rows": [{"cells": [f"Row {i}: a longer label for the sheet", i * 10.5],
                            "secondary": "a second line under it"} for i in range(60)]}}


class Peer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True

    def events(self, kind):
        return [f["params"]["payload"] for f in self.frames if f.get("params", {}).get("type") == kind]


@pytest.fixture
def cards_on():
    (get_hermes_home() / "config.yaml").write_text("cards:\n  enabled: true\n", encoding="utf-8")
    invalidate_check_fn_cache()
    yield
    invalidate_check_fn_cache()


def dispatched(card, call_id, sid):
    """The receipt exactly as the agent's tool executor gets it."""
    return model_tools.handle_function_call("show_card", card, "task", tool_call_id=call_id, session_id=sid,
                                            turn_id=f"turn-{call_id}")


def rpc(method, **params):
    return server.handle_request({"id": "cards-test", "method": method, "params": params})


def test_capability_round_trip(monkeypatch, cards_on):
    peer = Peer()
    monkeypatch.setattr(server, "_caller_transport", lambda: peer, raising=False)
    features = rpc("mobile.capabilities")["result"]
    assert "cards" in features["features"] and features["feature_versions"]["cards"] == 1

    reply = rpc("client.capabilities", cards={"v": 1, "kinds": KINDS})
    assert "error" not in reply, reply
    assert reply["result"]["cards"] == 1
    assert cards_capability.draws_cards(peer)

    rpc("client.capabilities", server_requests=True)  # an unrelated advertisement keeps it
    assert cards_capability.draws_cards(peer)
    rpc("client.capabilities", cards=None)
    assert not cards_capability.draws_cards(peer)


def test_capability_reply_says_zero_while_cards_are_off(monkeypatch):
    invalidate_check_fn_cache()
    peer = Peer()
    monkeypatch.setattr(server, "_caller_transport", lambda: peer, raising=False)
    assert rpc("client.capabilities", cards={"v": 1, "kinds": KINDS})["result"]["cards"] == 0


def test_a_future_version_is_not_drawn(monkeypatch):
    peer = Peer()
    monkeypatch.setattr(server, "_caller_transport", lambda: peer, raising=False)
    rpc("client.capabilities", cards={"v": 2, "kinds": ["hologram"]})
    assert not cards_capability.draws_cards(peer)


def test_toolset_fold_in_follows_the_attached_clients(cards_on):
    phone, desktop = Peer(), Peer()
    cards_capability.advertise(phone, {"v": 1, "kinds": KINDS})
    assert cards_capability.with_cards_toolset(["file"], {"transport": desktop}) == ["file"]
    assert cards_capability.with_cards_toolset(["file"], {"transport": phone}) == ["file", "cards"]
    assert cards_capability.with_cards_toolset(["file"], {"transport": FanoutTransport(desktop, phone)}) == ["file", "cards"]
    assert cards_capability.with_cards_toolset([], {"transport": phone}) == []  # explicit zero tools stays zero
    everything = cards_capability.with_cards_toolset(None, {"transport": phone})
    assert "cards" in everything and "terminal" in everything and "threads" not in everything
    assert cards_capability.with_cards_toolset(None, None) is None


def test_flag_off_never_folds_cards_in_even_for_a_drawing_client():
    invalidate_check_fn_cache()
    phone = Peer()
    cards_capability.advertise(phone, {"v": 1, "kinds": KINDS})
    assert cards_capability.with_cards_toolset(["file"], {"transport": phone}) == ["file"]
    assert cards_capability.carry_cards_toolset(["file"], SimpleNamespace(enabled_toolsets=["file", "cards"])) == ["file"]


def test_a_tool_refresh_keeps_the_cards_the_agent_was_built_with(cards_on):
    built = SimpleNamespace(enabled_toolsets=["file", "cards"])
    assert cards_capability.carry_cards_toolset(["file"], built) == ["file", "cards"]
    assert cards_capability.carry_cards_toolset(["file"], SimpleNamespace(enabled_toolsets=["file"])) == ["file"]


def test_the_snapshot_carries_every_card_the_validator_accepts():
    """Measured as the validator measures (compact JSON): a card near 16 KB compact is still carried."""
    from tools.cards.validate import MAX_CARD_BYTES, card_bytes, validate_card
    rows = [{"cells": ["x" * 16 for _ in range(12)]} for _ in range(60)]  # 720 cells: 720 commas default spacing widens
    card = {"kind": "table", "title": "Near the limit",
            "data": {"columns": [{"title": f"C{n}"} for n in range(12)], "rows": rows}}
    cells = [(row, index) for row in rows for index in range(12)]
    while len(card_bytes(card)) < MAX_CARD_BYTES - 40:
        row, index = cells.pop()
        row["cells"][index] += "y" * 4
    assert validate_card(card) == [] and len(json.dumps(card, ensure_ascii=False).encode()) > MAX_CARD_BYTES
    session, seq = {}, iter(range(1, 10))

    def frame(kind, payload):
        return {"method": "event", "params": {"type": kind, "seq": next(seq), "payload": payload}}
    project_event(session, frame("message.start", {}))
    project_event(session, frame("tool.start", {"tool_id": "t", "name": "show_card", "args": card}))
    assert session["_published_stream"]["parts"][0]["card"] == card


@pytest.mark.parametrize(("platform", "advertised", "offered"), [
    ("mobile", True, True), ("tui", True, True), ("mobile", False, False), ("telegram", True, False),
])
def test_a_built_agent_carries_show_card_only_for_a_drawing_client(cards_on, platform, advertised, offered):
    from run_agent import AIAgent
    peer = Peer()
    if advertised:
        cards_capability.advertise(peer, {"v": 1, "kinds": KINDS})
    agent = AIAgent(model="test-model", api_key="test-key", base_url="http://127.0.0.1:9/v1", quiet_mode=True,
                    enabled_toolsets=cards_capability.with_cards_toolset(["file"], {"transport": peer}),
                    platform=platform, skip_memory=True, skip_context_files=True)
    assert ("show_card" in agent.valid_tool_names) is offered
    assert "read_file" in agent.valid_tool_names


def test_card_arrives_live_with_tool_progress_off(monkeypatch, cards_on):
    peer = Peer()
    sid = "cards-live"
    session = {"transport": peer, "tool_progress_mode": "off", "show_reasoning": False,
               "tool_started_at": {}, "edit_snapshots": {}, "agent": SimpleNamespace(reasoning_config={})}
    monkeypatch.setitem(server._sessions, sid, session)
    assert len(json.dumps(SHEET)) > 4096

    server._emit("message.start", sid, {})
    server._on_tool_start(sid, "call-sheet", "show_card", SHEET)
    server._on_tool_complete(sid, "call-sheet", "show_card", SHEET, dispatched(SHEET, "call-sheet", sid))
    server._on_tool_start(sid, "call-read", "read_file", {"path": "README.md"})
    server._on_tool_complete(sid, "call-read", "read_file", {"path": "README.md"}, "contents")

    starts, completes = peer.events("tool.start"), peer.events("tool.complete")
    assert [p["tool_id"] for p in starts] == ["call-sheet"] and [p["tool_id"] for p in completes] == ["call-sheet"]
    assert starts[0]["args"] == SHEET and completes[0]["args"] == SHEET
    assert completes[0]["result"]["ok"] is True and completes[0]["result"]["card_id"] == "call-sheet"
    assert completes[0]["result"]["alt"].startswith("Sixty rows\n")

    # A phone that reconnects mid-turn rebuilds the card from the snapshot, whole.
    part = next(p for p in stream_cut(sid, session)["stream"]["parts"] if p.get("tool_id") == "call-sheet")
    assert part["card"] == SHEET and part["status"] == "complete" and part["error"] is False
    from tui_gateway.contracts.sessions import SessionStreamSnapshotResult
    SessionStreamSnapshotResult.model_validate(stream_cut(sid, session))


def test_a_refused_card_arrives_marked_as_an_error(monkeypatch, cards_on):
    peer = Peer()
    sid = "cards-refused"
    monkeypatch.setitem(server._sessions, sid, {"transport": peer, "tool_progress_mode": "off", "tool_started_at": {},
                                                "edit_snapshots": {}, "agent": SimpleNamespace(reasoning_config={})})
    bad = {"kind": "route", "title": "One stop", "data": {"stops": [{"name": "YUL", "lat": 45.4, "lon": -73.7}]}}
    server._emit("message.start", sid, {})
    server._on_tool_start(sid, "call-bad", "show_card", bad)
    server._on_tool_complete(sid, "call-bad", "show_card", bad, dispatched(bad, "call-bad", sid))
    assert peer.events("tool.complete")[0]["result"]["ok"] is False
    part = next(p for p in stream_cut(sid, server._sessions[sid])["stream"]["parts"] if p.get("tool_id") == "call-bad")
    assert part["error"] is True


def test_an_oversized_start_never_bloats_the_snapshot():
    session, frame_seq = {}, iter(range(1, 10))

    def frame(kind, payload):
        return {"method": "event", "params": {"type": kind, "seq": next(frame_seq), "payload": payload}}
    project_event(session, frame("message.start", {}))
    huge = {"kind": "table", "title": "x", "data": {"padding": "x" * 20000}}
    project_event(session, frame("tool.start", {"tool_id": "t", "name": "show_card", "args": huge}))
    part = session["_published_stream"]["parts"][0]
    assert "card" not in part and len(part["args_text"].encode()) <= 4096
