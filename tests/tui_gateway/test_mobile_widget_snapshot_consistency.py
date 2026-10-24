"""Inbox pages depend on their projection, not unrelated SQLite commits."""
import json

import pytest
from fastapi.testclient import TestClient

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer  # noqa: PLC0414 - pytest fixtures
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service  # noqa: PLC0414 - pytest fixture
from tests.tui_gateway.test_mobile_widget_inbox import register
from tui_gateway import mobile_widget_inbox


@pytest.fixture
def inbox_reader(push_service, mobile_home, peer, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr("pathlib.Path.home", lambda: mobile_home.parent)
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        for n in range(105):
            db.create_session(f"chat-{n:03}", "desktop")
            db.append_message(f"chat-{n:03}", "assistant", "fixture reply", timestamp=100 + n)
        db.create_session("unrelated", "desktop")
        db._conn.execute("UPDATE sessions SET hidden=1 WHERE id='unrelated'")
        db._conn.commit()
    params, _ = register(peer)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    client = TestClient(web_server.app)
    headers = {"Authorization": "Bearer " + params["read_token"]}

    def read(cursor=None):
        query = {"inbox": "1"}
        if cursor:
            query["cursor"] = cursor
        return client.get("/api/mobile/widgets/snapshot", params=query, headers=headers)

    return path, read, push_service.store, params["read_token"]


def test_unrelated_chat_append_preserves_cursor_and_revision(inbox_reader):
    path, read, store, token = inbox_reader
    first = read()
    assert first.status_code == 200
    page = first.json()
    assert len(page["items"]) == 100 and page["next_cursor"]
    with SessionDB(db_path=path) as writer:
        writer.append_message("unrelated", "assistant", "unrelated reply")
    second = read(page["next_cursor"])
    assert second.status_code == 200, second.text
    assert second.json()["revision"] == page["revision"]
    items = page["items"] + second.json()["items"]
    assert len({item["session_id"] for item in items}) == 106
    assert "unrelated" not in {item["session_id"] for item in items}
    assert second.json()["next_cursor"] is None
    assert mobile_widget_inbox.inbox_snapshot(store, token)["revision"] == page["revision"]


def test_unrelated_commit_between_list_and_replies_is_not_a_conflict(inbox_reader, monkeypatch):
    path, read, _, _ = inbox_reader
    original = mobile_widget_inbox.page_replies

    def concurrent_reply(db, rows):
        with SessionDB(db_path=path) as writer:
            writer.append_message("unrelated", "assistant", "concurrent reply")
        return original(db, rows)

    monkeypatch.setattr(mobile_widget_inbox, "page_replies", concurrent_reply)
    response = read()
    assert response.status_code == 200, response.text
    chats = [item for item in response.json()["items"] if item["session_id"].startswith("chat-")]
    assert len(chats) == 99
    assert all(item["reply"] is not None for item in chats)


def test_page_keeps_one_sqlite_cut_when_visible_reply_changes(inbox_reader, monkeypatch):
    path, read, _, _ = inbox_reader
    before = read().json()
    original = mobile_widget_inbox.page_replies

    def concurrent_reply(db, rows):
        with SessionDB(db_path=path) as writer:
            writer.append_message("chat-104", "assistant", "new visible reply", timestamp=1000)
        return original(db, rows)

    monkeypatch.setattr(mobile_widget_inbox, "page_replies", concurrent_reply)
    response = read()
    assert response.status_code == 200, response.text
    assert response.json()["items"] == before["items"]
    assert response.json()["revision"] == before["revision"]
    monkeypatch.setattr(mobile_widget_inbox, "page_replies", original)
    assert read().json()["revision"] != before["revision"]


@pytest.mark.parametrize("change", ["title", "read", "reply", "archive", "delete"])
def test_relevant_projection_change_refuses_old_cursor(inbox_reader, change):
    path, read, _, _ = inbox_reader
    first = read().json()
    with SessionDB(db_path=path) as writer:
        if change == "reply":
            writer.append_message("chat-000", "assistant", "new reply", timestamp=1000)
        elif change == "delete":
            writer.delete_session("chat-000")
        else:
            sql = {
                "title": "UPDATE sessions SET title='Renamed' WHERE id='chat-000'",
                "read": "UPDATE sessions SET last_read_at=1000 WHERE id='chat-000'",
                "archive": "UPDATE sessions SET archived=1 WHERE id='chat-000'",
            }[change]
            writer._conn.execute(sql)
            writer._conn.commit()
    response = read(first["next_cursor"])
    assert response.status_code == 409
    assert read().status_code == 200


def test_epoch_change_during_read_fails_closed(inbox_reader, monkeypatch):
    _, read, _, _ = inbox_reader
    from tui_gateway import server_requests

    original = mobile_widget_inbox.page_replies

    def new_epoch(db, rows):
        result = original(db, rows)
        monkeypatch.setattr(server_requests, "inbox_epoch", lambda: "replacement-process-epoch")
        return result

    monkeypatch.setattr(mobile_widget_inbox, "page_replies", new_epoch)
    response = read()
    assert response.status_code == 409, json.loads(response.content)
