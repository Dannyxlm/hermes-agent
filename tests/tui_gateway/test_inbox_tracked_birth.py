"""A chat the phone or Desktop starts is read-tracked from birth, so it reaches the Inbox.

``last_read_at`` NULL means "never tracked", and the Inbox treats a never-tracked chat as read. The
first read stamp used to come only from the client's own PATCH, so a chat left before its first reply
landed never became unread. The creating client is identified by the connection's user agent (the
same identity ``route_metrics`` labels ``phone``/``desktop``), never by ``source``: the phone sends no
``source``, so its chats are stored as ``tui`` exactly like terminal chats, which must stay untracked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_session_resume_materializes_minted_key import MINTED_KEY, real_db as real_db  # noqa: PLC0414 - imported pytest fixture
from tui_gateway import server
from tui_gateway.inbox_summaries import page_replies
from tui_gateway.transport import bind_transport, reset_transport
from tui_gateway.ws import WSTransport

PHONE = b"HermesMobile/2026101345 CFNetwork/1568.100.1 Darwin/24.0.0"
DESKTOP = b"Mozilla/5.0 (Macintosh) Electron/33.0.0 Chrome/130.0.0.0 Hermes/1.4.2 Safari/537.36"
BROWSER = b"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def connection(loop, user_agent):
    ws = SimpleNamespace(scope={"headers": [(b"host", b"example"), (b"user-agent", user_agent)]})
    return WSTransport(ws, loop, peer="test")


@pytest.fixture
def gateway_db(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    yield db
    db.close()


def new_chat_first_prompt(transport):
    """``session.create`` as the app sends it (no ``source``), then the row write ``prompt.submit`` makes."""
    token = bind_transport(transport)
    try:
        resp = server.handle_request({"id": "c", "method": "session.create", "params": {"cols": 80}})
    finally:
        reset_transport(token)
    sid, key = resp["result"]["session_id"], resp["result"]["stored_session_id"]
    try:
        assert server._persist_session_row_for_submit("rid", server._sessions[sid], "hello", None) is None
    finally:
        server._sessions.pop(sid, None)
    return key


def inbox_unread_after_reply(db, key):
    """What the Inbox projection says once the first reply lands (the user already left the chat)."""
    db.append_message(key, "assistant", "the answer", finish_reason="stop")
    row = db.get_session(key)
    return page_replies(db, [{"id": key, "last_read_at": row["last_read_at"]}])[key]["unread"]


@pytest.mark.parametrize("user_agent", [PHONE, DESKTOP])
def test_chat_created_by_phone_or_desktop_reaches_the_inbox(gateway_db, loop, user_agent):
    key = new_chat_first_prompt(connection(loop, user_agent))
    row = gateway_db.get_session(key)
    assert row["source"] == "tui"  # the phone's chats are indistinguishable from terminal ones by source
    assert row["last_read_at"] == 0.0
    assert inbox_unread_after_reply(gateway_db, key) is True


@pytest.mark.parametrize("user_agent", [BROWSER, None])
def test_chat_created_by_other_clients_stays_untracked(gateway_db, loop, user_agent):
    transport = connection(loop, user_agent) if user_agent else SimpleNamespace(write=lambda frame: True)
    key = new_chat_first_prompt(transport)
    assert gateway_db.get_session(key)["last_read_at"] is None
    assert inbox_unread_after_reply(gateway_db, key) is False


def test_first_prompt_never_moves_an_existing_watermark(gateway_db, loop):
    transport = connection(loop, PHONE)
    token = bind_transport(transport)
    try:
        resp = server.handle_request({"id": "c", "method": "session.create", "params": {"cols": 80}})
    finally:
        reset_transport(token)
    sid, key = resp["result"]["session_id"], resp["result"]["stored_session_id"]
    try:
        session = server._sessions[sid]
        assert server._persist_session_row_for_submit("rid", session, "one", None) is None
        gateway_db.set_session_read(key, True)
        stamped = gateway_db.get_session(key)["last_read_at"]
        assert stamped > 0
        assert server._persist_session_row_for_submit("rid", session, "two", None) is None
    finally:
        server._sessions.pop(sid, None)
    assert gateway_db.get_session(key)["last_read_at"] == stamped


def test_minted_key_materialized_on_resume_by_the_phone_is_tracked(real_db, loop):
    token = bind_transport(connection(loop, PHONE))
    try:
        resp = server.handle_request({"id": "1", "method": "session.resume", "params": {"session_id": MINTED_KEY}})
    finally:
        reset_transport(token)
    assert "error" not in resp, resp
    assert real_db.get_session(MINTED_KEY)["last_read_at"] == 0.0


@pytest.mark.parametrize("user_agent", [PHONE, DESKTOP])
def test_resuming_an_existing_untracked_chat_leaves_it_untracked(real_db, loop, user_agent):
    real_db.create_session(MINTED_KEY, source="tui", model="test-model")
    token = bind_transport(connection(loop, user_agent))
    try:
        resp = server.handle_request({"id": "1", "method": "session.resume", "params": {"session_id": MINTED_KEY}})
    finally:
        reset_transport(token)
    assert "error" not in resp, resp
    assert real_db.get_session(MINTED_KEY)["last_read_at"] is None
