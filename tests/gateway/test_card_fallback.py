"""The messaging fallback for Paper cards: a card turn that reaches Telegram, Photon or BlueBubbles
arrives as the reply plus each card's ``alt`` as its own paragraph, never JSON, links intact.

The first test runs a real AIAgent (scripted provider, real ``show_card`` execution) through the
gateway's real response shaping; the others push the same text through each adapter's own send path.
"""
import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hermes_yaml as yaml
import run_agent
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from tests.agent import test_empty_response_recovery_persistence as recovery
from tests.gateway.test_gateway_silence_tokens import _runner
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter
from tools.cards.fallback import cards_text, turn_card_text, turn_cards
from tools.cards.tool import SHOW_CARD_SCHEMA

real_loop = recovery.real_loop
MAPS = "https://maps.apple.com/?saddr=45.4577,-73.7499&daddr=45.5075,-73.554&dirflg=d"
ROUTE = {"kind": "route", "title": "Airport to the hotel",
         "data": {"stops": [{"name": "YUL", "lat": 45.4577, "lon": -73.7499},
                            {"name": "Hôtel William Gray", "lat": 45.5075, "lon": -73.554}],
                  "duration": "24 min by car", "distance": "21 km"}}
ROUTE_ALT = f"Airport to the hotel: YUL → Hôtel William Gray, 24 min by car, 21 km.\n{MAPS}"
LINK = {"kind": "link", "title": "Loft near the canal", "data": {"url": "https://www.airbnb.com/rooms/000000?check_in=2027-01-14"},
        "alt": "Loft near the canal\n\nEntire loft, 2 guests\nhttps://www.airbnb.com/rooms/000000?check_in=2027-01-14"}
REPLY = "It is 24 minutes by car; leave by 15:10."


def _turn(*cards, refused=()):
    """An assistant step with show_card calls, their receipts, then the closing reply."""
    calls = [{"id": f"call-{i}", "type": "function", "function": {"name": "show_card", "arguments": json.dumps(card)}}
             for i, card in enumerate([*cards, *refused])]
    rows = [{"role": "assistant", "content": "", "tool_calls": calls}]
    for i, card in enumerate(cards):
        rows.append({"role": "tool", "tool_call_id": f"call-{i}", "content": json.dumps(
            {"ok": True, "card_id": f"call-{i}", "alt": card.get("alt") or ROUTE_ALT})})
    for j, _card in enumerate(refused, start=len(cards)):
        rows.append({"role": "tool", "tool_call_id": f"call-{j}", "content": json.dumps(
            {"ok": False, "error": "card refused", "errors": [{"path": "title", "message": "x"}]})})
    rows.append({"role": "assistant", "content": REPLY})
    return rows


def test_only_this_turns_accepted_cards_speak():
    old = _turn(ROUTE)
    new = [{"role": "user", "content": "and the loft?"}, *_turn(LINK, refused=[{"kind": "route", "title": "bad"}])]
    result = {"messages": [*old, *new], "history_offset": len(old)}
    assert [card["card_id"] for card in turn_cards(result["messages"][len(old):])] == ["call-0"]
    assert turn_card_text(result, old) == "Loft near the canal\nEntire loft, 2 guests\nhttps://www.airbnb.com/rooms/000000?check_in=2027-01-14"
    assert turn_card_text({"messages": _turn(ROUTE, LINK)}, []) == ROUTE_ALT + "\n\n" + cards_text([{"alt": LINK["alt"]}])
    assert turn_card_text({"messages": [{"role": "assistant", "content": REPLY}]}, []) == ""


@pytest.mark.asyncio
async def test_real_card_turn_reaches_a_messaging_chat_as_words(monkeypatch, tmp_path, real_loop):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"display": {"tool_progress": "off"}, "cards": {"enabled": True}}))
    from hermes_constants import get_hermes_home
    from tools.registry import invalidate_check_fn_cache
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({"cards": {"enabled": True}}))  # the handler's gate
    invalidate_check_fn_cache()
    agent = real_loop.agent
    agent.valid_tool_names.add("show_card")
    agent.tools.append({"type": "function", "function": SHOW_CARD_SCHEMA})
    script = [recovery._response("", finish_reason="tool_calls", tool_calls=[recovery._call("show_card", ROUTE, "call_card")]),
              recovery._response(REPLY)]
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = lambda **_kw: script.pop(0)

    class IsolatedAgent(run_agent.AIAgent):
        def __new__(cls, **kwargs):
            for key, value in kwargs.items():
                if key.endswith("callback"):
                    setattr(agent, key, value)
            return agent

    monkeypatch.setattr(run_agent, "AIAgent", IsolatedAgent)
    adapter = ProgressCaptureAdapter(platform=Platform.TELEGRAM)
    runner = _runner(monkeypatch, tmp_path)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store.get_model_override.return_value = None
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="dm-card", chat_type="dm", user_id="human")
    key = "agent:main:telegram:dm:dm-card"
    entry = runner.session_store.get_or_create_session.return_value
    entry.session_key, entry.session_id, entry.platform, entry.chat_type = key, real_loop.sid, Platform.TELEGRAM, "dm"
    event = MessageEvent(text="how do I get to the hotel?", source=source, message_id="m1", reply_expected=True)

    reply = await runner._handle_message_with_agent(event, source, key, 1)
    delivered = "\n".join([reply or "", *(call["content"] for call in adapter.sent)])
    assert not script, "the real agent must consume the whole scripted provider sequence"
    assert reply.startswith(REPLY) and ROUTE_ALT in reply
    assert '{"' not in delivered and "show_card" not in delivered
    # The stored turn keeps the call and its receipt; the words exist only on the way out.
    stored = real_loop.db.get_messages(real_loop.sid)
    assert any(m.get("tool_calls") and "show_card" in json.dumps(m["tool_calls"]) for m in stored)
    assert all(ROUTE_ALT not in (m.get("content") or "") for m in stored if m.get("role") == "assistant")


@pytest.mark.parametrize(("platform", "silence", "has_cards"), [
    (Platform.TELEGRAM, False, True), (Platform.LOCAL, False, False), (Platform.TELEGRAM, True, False),
])
def test_trailing_lines_skip_local_viewers_and_silence(platform, silence, has_cards):
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
    runner = SimpleNamespace(_hmwa_runtime_footer_line=lambda *a: "— model · 3s")
    text = GatewayTurnPersistenceMixin._hmwa_trailing_lines(
        runner, {"messages": _turn(ROUTE)}, [], SimpleNamespace(platform=platform), 3.0, silence)
    assert text == (ROUTE_ALT + "\n\n— model · 3s" if has_cards else "— model · 3s")


@pytest.mark.asyncio
async def test_bluebubbles_sends_each_card_as_its_own_bubble(monkeypatch):
    from tests.gateway.test_bluebubbles_plugin import _make_adapter
    adapter = _make_adapter(monkeypatch)
    bubbles = []

    async def resolve(_chat_id):
        return "iMessage;-;+15145550100"

    async def post(_path, payload):
        bubbles.append(payload["message"])
        return SendResult(success=True, message_id="guid")

    monkeypatch.setattr(adapter, "_resolve_chat_guid", resolve)
    monkeypatch.setattr(adapter, "_post_message", post)
    await adapter.send("+15145550100", REPLY + "\n\n" + turn_card_text({"messages": _turn(ROUTE, LINK)}, []))
    assert bubbles == [REPLY, ROUTE_ALT, "Loft near the canal\nEntire loft, 2 guests\nhttps://www.airbnb.com/rooms/000000?check_in=2027-01-14"]


@pytest.mark.asyncio
@pytest.mark.parametrize("markdown", ["true", "false"])
async def test_photon_keeps_the_words_and_links(monkeypatch, markdown):
    monkeypatch.setenv("PHOTON_PROJECT_ID", "pid")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "psecret")
    monkeypatch.setenv("PHOTON_MARKDOWN", markdown)
    from plugins.platforms.photon import adapter as photon
    monkeypatch.setattr(photon, "find_node_executable", lambda _name: "/pm/node")
    adapter = photon.PhotonAdapter(PlatformConfig(enabled=True, token="", extra={}))
    sent = []

    async def sidecar_send(_chat_id, text):
        sent.append(text)
        return SendResult(success=True, message_id="p1")

    monkeypatch.setattr(adapter, "_sidecar_send", sidecar_send)
    await adapter.send("space-1", REPLY + "\n\n" + turn_card_text({"messages": _turn(ROUTE)}, []))
    assert sent == [REPLY + "\n\n" + ROUTE_ALT]


def test_telegram_renders_the_same_words_with_links_intact():
    from plugins.platforms.telegram.telegram_markdown import to_markdown_v2
    text = REPLY + "\n\n" + turn_card_text({"messages": _turn(ROUTE, LINK)}, [])
    rendered = re.sub(r"\\(.)", r"\1", to_markdown_v2(text))  # what Telegram shows after MarkdownV2 escapes
    assert MAPS in rendered and "https://www.airbnb.com/rooms/000000?check_in=2027-01-14" in rendered
    assert "YUL → Hôtel William Gray" in rendered and '{"' not in rendered


def _rows(n, start=0):
    """n accepted cards, ids call-{start}…, each its own little turn after a user row."""
    rows = []
    for i in range(start, start + n):
        card = {**LINK, "title": f"Card {i}", "alt": f"Card {i}"}
        rows += [{"role": "user", "content": f"q{i}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"call-{i}", "type": "function", "function": {
                     "name": "show_card", "arguments": json.dumps(card)}}]},
                 {"role": "tool", "tool_call_id": f"call-{i}", "content": json.dumps({"ok": True, "alt": card["alt"]})},
                 {"role": "assistant", "content": "ok"}]
    return rows


@pytest.mark.parametrize("offset", [0, 999, None, "3"])
def test_a_compacted_turn_speaks_only_its_own_cards(offset):
    """Review 4b: after a mid-turn compaction the gateway reports history_offset 0 (or an offset that no
    longer fits); the turn is then the messages from its last user row on."""
    messages = _rows(7) + _rows(1, start=7)
    result = {"messages": messages, **({"history_offset": offset} if offset is not None else {})}
    assert turn_card_text(result, history=[{}] * 999 if offset is None else []) == "Card 7"


def test_the_same_call_id_twice_counts_once():
    rows = _rows(1) + _rows(1)
    rows[-2]["content"] = json.dumps({"ok": True, "alt": "a different receipt"})
    assert [c["alt"] for c in turn_cards(rows)] == ["Card 0"]


@pytest.mark.parametrize("messages", [
    [{"role": "assistant", "tool_calls": ["show_card", None, 3]}],
    [{"role": "assistant", "tool_calls": "show_card"}],
    [{"role": "assistant", "tool_calls": [{"id": "x", "function": "show_card"}]}, {"role": "tool", "tool_call_id": "x"}],
    [None, "user", {"role": "tool", "tool_call_id": ["x"], "content": {"ok": True}}],
])
def test_odd_shapes_never_raise_into_a_reply(messages):
    assert turn_card_text({"messages": messages, "history_offset": 0}, []) == ""


def test_trailing_lines_survive_a_card_words_failure(monkeypatch):
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
    from tools.cards import fallback

    def broken(*_a):
        raise RuntimeError("card words broke")

    monkeypatch.setattr(fallback, "turn_card_text", broken)
    runner = SimpleNamespace(_hmwa_runtime_footer_line=lambda *a: "— model · 3s")
    assert GatewayTurnPersistenceMixin._hmwa_trailing_lines(
        runner, {"messages": _turn(ROUTE)}, [], SimpleNamespace(platform=Platform.TELEGRAM), 3.0, False) == "— model · 3s"


def test_card_words_never_become_an_attachment():
    """Review 4a. Joined fields can form a directive the validator never saw in one field (a route titled
    MEDIA whose first stop is named like a file), and an old receipt can carry one: the gateway's real
    ``extract_media`` must find nothing in what the fallback sends."""
    from gateway.platforms.base import BasePlatformAdapter
    from tools.cards.alt import card_alt
    from tools.cards.validate import validate_card
    leak = "/synthetic/cards/lease-renewal.pdf"  # extract_media takes a path whether or not it exists
    joined = {"kind": "route", "title": "MEDIA", "data": {"mode": "flight", "stops": [
        {"name": leak, "lat": 45.5, "lon": -73.6}, {"name": "B", "lat": 45.6, "lon": -73.5}]}}
    assert validate_card(joined) == []
    assert BasePlatformAdapter.extract_media(f"MEDIA: {leak}")[0], "the gateway does attach a bare directive"
    old_receipt = {"alt": f"Re: lease MEDIA:{leak} [[as_document]]"}
    text = REPLY + "\n\n" + cards_text([{"alt": card_alt(joined)}, old_receipt])
    media, cleaned = BasePlatformAdapter.extract_media(text)
    assert media == [] and "lease-renewal.pdf" in cleaned


def test_ordinary_media_words_go_out_as_written():
    """Review delta D1: Danny's paid-media cards keep their words; the gateway attaches nothing for them."""
    from gateway.platforms.base import BasePlatformAdapter
    words = "Paid media: November results\nSocial media: what changed\nMedia: CBC News\nhttps://en.wikipedia.org/wiki/Media:Foo.ogg"
    assert cards_text([{"alt": words}]) == words
    assert BasePlatformAdapter.extract_media(REPLY + "\n\n" + words)[0] == []
