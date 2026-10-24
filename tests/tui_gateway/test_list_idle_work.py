"""Bound background observation and latest-reply work, preserving reply identity."""
import sqlite3

import pytest

from hermes_state import SessionDB
from tui_gateway import server, session_change_cursor
from tui_gateway.inbox_summaries import page_replies


@pytest.mark.parametrize("stdio,peers", [(False, set()), (True, set()), (False, {object()})])
def test_watcher_observes_only_with_ui_demand(tmp_path, monkeypatch, stdio, peers):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('one', 'Original')")
    import hermes_state
    opened = []
    connect = hermes_state._connect_tracked_db

    def track(*args, **kwargs):
        opened.append(args[0])
        return connect(*args, **kwargs)

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", track)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    monkeypatch.setattr(server, "_change_sigs", {})
    monkeypatch.setattr(server, "_change_checked_at", {})
    monkeypatch.setattr(server, "_live_transports", peers)
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", stdio)
    monkeypatch.setattr(server, "_CHANGE_WATCHES", {"sessions.changed": (0.5, server._sessions_sig, dict)})
    server._broadcast_watched_changes(now=10)
    assert bool(opened) == bool(stdio or peers)


def test_listener_refresh_survives_no_ui(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, source TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('one', 'Original', 'desktop')")
    monkeypatch.setattr(server, "_live_transports", set())
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    changes = []
    monkeypatch.setattr(session_change_cursor, "_listeners", [changes.append])
    session_change_cursor.refresh(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title='Renamed'")
    session_change_cursor.refresh(path)
    assert len(changes) == 1


def test_latest_reply_seeks_stop_before_old_transcript(tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("root", "desktop")
        old = db.append_message("root", "assistant", "compacted reply", timestamp=100)
        db._conn.execute("UPDATE messages SET active=0, compacted=1 WHERE id=?", (old,))
        db._conn.execute("UPDATE sessions SET ended_at=101, end_reason='compression' WHERE id='root'")
        db.create_session("tip", "desktop", parent_session_id="root")
        db._conn.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp, active, compacted) "
            "VALUES ('tip', 'assistant', 'older reply', 102, 1, 0)", [()] * 5000)
        final = db.append_message("tip", "assistant", "latest reply", timestamp=103)
        db.append_message("tip", "assistant", "tool preamble", timestamp=104, finish_reason="tool_calls")
        db.append_message("tip", "assistant", " \t\r\n", timestamp=105)
        db._conn.commit()
        steps = []
        db._conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        try:
            result = page_replies(db, [
                {"id": "root", "_lineage_ids": ["root"], "last_read_at": 99},
                {"id": "tip", "_lineage_ids": ["root", "tip"], "last_read_at": 103},
                {"id": "missing"},
            ])
        finally:
            db._conn.set_progress_handler(None, 0)
        assert result == {
            "root": {"row_id": old, "at": 100, "unread": True, "preview": "compacted reply"},
            "tip": {"row_id": final, "at": 103, "unread": False, "preview": "latest reply"},
        }
        assert len(steps) < 100, "A latest-reply read must not walk the historical transcript"
