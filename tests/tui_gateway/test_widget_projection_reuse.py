"""A coherent Inbox cut reuses durable work without hiding relevant changes."""
import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_mobile_widget_snapshot_consistency import inbox_reader as inbox_reader  # noqa: PLC0414
from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer  # noqa: PLC0414
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service  # noqa: PLC0414
from tui_gateway import mobile_widget_inbox as inbox


def test_pages_and_unrelated_writes_reuse_one_projection(inbox_reader, monkeypatch):
    path, read, _, _ = inbox_reader
    calls = []
    original = SessionDB.list_sessions_rich

    def listing(db, *args, **kwargs):
        calls.append(1)
        return original(db, *args, **kwargs)

    monkeypatch.setattr(SessionDB, "list_sessions_rich", listing)
    first = read().json()
    with SessionDB(db_path=path) as writer:
        writer.append_message("unrelated", "assistant", "excluded write")
    second = read(first["next_cursor"])
    assert second.status_code == 200
    assert second.json()["revision"] == first["revision"]
    assert read().json()["items"] == first["items"]
    assert len(calls) == 1, "unchanged Inbox pages rebuilt durable projection"
    with SessionDB(db_path=path) as writer:
        writer._conn.execute("UPDATE sessions SET title='Changed' WHERE id='chat-000'")
        writer._conn.commit()
    assert read(first["next_cursor"]).status_code == 409
    fresh = read().json()
    assert fresh["revision"] != first["revision"]
    assert read(fresh["next_cursor"]).status_code == 200
    assert len(calls) == 2, "one relevant change must rebuild exactly once"


def test_cached_attention_is_not_frozen(inbox_reader, monkeypatch):
    _, read, _, _ = inbox_reader
    first = read().json()
    monkeypatch.setattr(inbox, "page_attention", lambda rows, home: {
        "chat-104": {"kind": "approval", "count": 1, "revision": "new-request"}})
    assert read(first["next_cursor"]).status_code == 409
    fresh = read().json()
    assert fresh["revision"] != first["revision"]
    assert any(item["attention"] for item in fresh["items"])


@pytest.mark.parametrize("change", ["rewrite", "delete_reply"])
def test_cached_reply_refreshes_on_in_place_transcript_changes(inbox_reader, change):
    path, read, _, _ = inbox_reader
    first = read().json()
    with SessionDB(db_path=path) as writer:
        if change == "rewrite":
            writer._conn.execute("UPDATE messages SET timestamp=9000 WHERE session_id='chat-000'")
        else:
            writer._conn.execute("DELETE FROM messages WHERE session_id='chat-000'")
        writer._conn.commit()
    assert read(first["next_cursor"]).status_code == 409
    assert read().json()["revision"] != first["revision"]
