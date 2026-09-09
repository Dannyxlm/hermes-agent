"""Real SQLite freshness and full-fidelity pagination across short-lived mobile readers."""

import sqlite3

import pytest

from hermes_state import SessionDB
from tui_gateway import mobile_history_cache, server


@pytest.fixture
def history_cache(monkeypatch):
    cache = mobile_history_cache.HistoryPageCache()
    monkeypatch.setattr(mobile_history_cache, "history_pages", cache)
    yield cache
    cache.clear()


def _page(home, *, tip="root", chain=None, **params):
    with server._mobile_read_db(home) as reader:
        return server._mobile_history_page(reader, tip, chain or [tip], params)


def test_repeated_pages_skip_lineage_work_but_commits_refresh_every_byte(tmp_path, monkeypatch, history_cache):
    from agent import context_compressor

    visits = []
    split = context_compressor.split_user_originated_turn
    def count_decode(message):
        visits.append(message["content"])
        return split(message)
    monkeypatch.setattr(context_compressor, "split_user_originated_turn", count_decode)
    text = "complete original text " * 1000
    with SessionDB(db_path=tmp_path / "state.db") as writer:
        writer.create_session("root", "desktop")
        writer.append_message("root", "user", text, timestamp=10)
        writer.append_message("root", "assistant", "middle", timestamp=20)
        writer.append_message("root", "user", "latest", timestamp=30)
        first = _page(tmp_path, limit=1)
        cursor = first["before_row_id"]
        oldest = _page(tmp_path, limit=2, before_row_id=cursor)
        assert [m["text"] for m in oldest["messages"]] == [text.strip(), "middle"]
        decoded = len(visits)
        assert decoded > 0
        assert _page(tmp_path, limit=1) == first
        assert _page(tmp_path, limit=2, before_row_id=cursor) == oldest
        assert len(visits) == decoded  # Fresh handles still avoid re-decoding the lineage.

        # The writer is distinct from the cache's strictly read-only version watcher.
        writer._conn.execute("UPDATE messages SET content='edited middle' WHERE timestamp=20")
        assert _page(tmp_path, limit=2, before_row_id=cursor)["messages"][-1]["text"] == "edited middle"
        writer._conn.execute("DELETE FROM messages WHERE timestamp=20")
        assert [m["text"] for m in _page(tmp_path, limit=2, before_row_id=cursor)["messages"]] == [text.strip()]

        # A newer compaction generation wins without moving the logical cursor.
        writer._conn.execute("UPDATE messages SET active=0, compacted=1 WHERE timestamp=10")
        writer.append_message("root", "user", text, timestamp=10)
        compacted = _page(tmp_path, limit=1)
        assert compacted["before_row_id"] == cursor
        assert compacted["messages"][0]["text"] == "latest"
        earlier = _page(tmp_path, limit=1, before_row_id=cursor)
        assert earlier["messages"][0]["text"] == text.strip()
        assert not earlier["has_more"]
        assert earlier["messages"][0]["row_id"] > oldest["messages"][0]["row_id"]

        writer.create_session("tip", "desktop", parent_session_id="root")
        writer.append_message("tip", "tool", "complete tool output", tool_call_id="tool-1", tool_name="terminal")
        lineage = _page(tmp_path, tip="tip", chain=["root", "tip"], limit=100)
        assert [m["text"] for m in lineage["messages"]] == [text.strip(), "latest", "complete tool output"]
        writer.append_message("tip", "assistant", "new terminal response")
        assert _page(tmp_path, tip="tip", chain=["root", "tip"], limit=100)["messages"][-1]["text"] == "new terminal response"
        assert [m["text"] for m in _page(tmp_path, limit=100)["messages"]] == [text.strip(), "latest"]
        other_home = tmp_path / "other-profile"
        with SessionDB(db_path=other_home / "state.db") as other:
            other.create_session("root", "desktop")
            other.append_message("root", "user", "same ID on another profile")
        assert _page(other_home)["messages"][0]["text"] == "same ID on another profile"
        assert [m["text"] for m in _page(tmp_path, limit=100)["messages"]] == [text.strip(), "latest"]
        watcher = next(iter(history_cache._databases.values())).db
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            watcher._conn.execute("UPDATE messages SET content='forbidden'")


def test_cache_never_publishes_racing_uncommitted_or_replaced_data(tmp_path, monkeypatch, history_cache):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as writer:
        writer.create_session("root", "desktop")
        writer.append_message("root", "user", "old committed text")
        publish = history_cache.publish
        def commit_before_publication(token, key, rows):
            writer._conn.execute("UPDATE messages SET content='committed during read'")
            publish(token, key, rows)
        monkeypatch.setattr(history_cache, "publish", commit_before_publication)
        assert _page(tmp_path)["messages"][0]["text"] == "old committed text"
        monkeypatch.setattr(history_cache, "publish", publish)
        assert _page(tmp_path)["messages"][0]["text"] == "committed during read"

        # An uncommitted writer read must not consume or populate committed page entries.
        writer._conn.execute("BEGIN")
        writer._conn.execute("UPDATE messages SET content='uncommitted text'")
        assert server._mobile_history_page(writer, "root", ["root"], {})["messages"][0]["text"] == "uncommitted text"
        writer._conn.execute("ROLLBACK")
        assert _page(tmp_path)["messages"][0]["text"] == "committed during read"

    replacement = tmp_path / "replacement.db"
    with SessionDB(db_path=replacement) as writer:
        writer.create_session("root", "desktop")
        writer.append_message("root", "user", "replacement generation")
    replacement.replace(path)
    assert _page(tmp_path)["messages"][0]["text"] == "replacement generation"

    # Oversized pages retain all text without requiring unbounded cache retention.
    huge = "full content " * (2 * 1024 * 1024 // 13)
    with SessionDB(db_path=path) as writer:
        writer._conn.execute("UPDATE messages SET content=?", (huge,))
    assert _page(tmp_path)["messages"][0]["text"] == huge.strip()
    assert _page(tmp_path)["messages"][0]["text"] == huge.strip()


def test_old_reader_cannot_populate_replacement_database_cache(tmp_path, history_cache):
    path = tmp_path / "state.db"
    replacement = tmp_path / "replacement.db"
    for target, text in ((path, "original file"), (replacement, "replacement file")):
        with SessionDB(db_path=target) as writer:
            writer.create_session("root", "desktop")
            writer.append_message("root", "user", text)

    with server._mobile_read_db(tmp_path) as old_reader:
        replacement.replace(path)
        assert server._mobile_history_page(old_reader, "root", ["root"], {})["messages"][0]["text"] == "original file"
        assert _page(tmp_path)["messages"][0]["text"] == "replacement file"
        # The old reader must also bypass the replacement's now-populated page.
        assert server._mobile_history_page(old_reader, "root", ["root"], {})["messages"][0]["text"] == "original file"
        assert _page(tmp_path)["messages"][0]["text"] == "replacement file"


@pytest.mark.parametrize("replace_during", ["reader", "watcher"])
def test_replacement_during_handle_open_closes_mismatched_reader(tmp_path, monkeypatch, history_cache, replace_during):
    path = tmp_path / "state.db"
    replacement = tmp_path / "replacement.db"
    for target, text in ((path, "original file"), (replacement, "replacement file")):
        with SessionDB(db_path=target) as writer:
            writer.create_session("root", "desktop")
            writer.append_message("root", "user", text)

    opened = []
    initialize = SessionDB.__init__
    def replace_after_open(db, *args, **kwargs):
        initialize(db, *args, **kwargs)
        if kwargs.get("read_only") and not opened:
            opened.append(db)
            replacement.replace(path)

    if replace_during == "reader":
        monkeypatch.setattr(SessionDB, "__init__", replace_after_open)
        with pytest.raises(RuntimeError, match="replaced during open"):
            _page(tmp_path)
    else:
        with server._mobile_read_db(tmp_path) as reader:
            monkeypatch.setattr(SessionDB, "__init__", replace_after_open)
            assert server._mobile_history_page(reader, "root", ["root"], {})["messages"][0]["text"] == "original file"

    assert opened[0]._conn is None  # The mismatched read-only handle must not leak.
    assert _page(tmp_path)["messages"][0]["text"] == "replacement file"
