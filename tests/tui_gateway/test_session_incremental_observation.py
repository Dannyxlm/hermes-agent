"""Session observations must cost proportional to committed projection changes, not history."""

import sqlite3

import pytest

from hermes_state import SessionDB
from tui_gateway import server, session_change_cursor


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db._conn.executemany(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, 'desktop', 1)",
            [(f"s{i:05}",) for i in range(3000)])
        db._conn.commit()
    session_change_cursor.reset_for_tests()
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    yield path
    server._drop_store(str(path))
    session_change_cursor.reset_for_tests()


def test_single_row_edits_and_unrelated_commits_have_bounded_observation_work(store, monkeypatch):
    import hermes_state
    real = hermes_state._connect_tracked_db
    work = []

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_progress_handler(lambda: work.append(1) or 0, 100)
        return conn

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", connect)
    server._session_db_content_sig(store)
    cursor = session_change_cursor.current_cursor(store)
    before = server._session_db_content_sig(store)
    with sqlite3.connect(store) as writer:
        writer.execute("UPDATE sessions SET title='edited', pinned=1, hidden=1, last_read_at=10 WHERE id='s00001'")
    work.clear()
    assert server._session_db_content_sig(store) != before
    assert session_change_cursor.changes_since(store, cursor)[0] == {"s00001"}
    assert len(work) < 50, "one edit must not execute a history-sized SQL scan"

    before = server._session_db_content_sig(store)
    with sqlite3.connect(store) as writer:
        writer.execute("UPDATE sessions SET last_activity_at=20 WHERE id='s00002'")
    work.clear()
    assert server._session_db_content_sig(store) == before
    assert len(work) < 50, "heartbeat commits must not scan session history"


def test_incremental_insert_delete_and_compaction_keep_complete_lineage(store):
    server._session_db_content_sig(store)
    cursor = session_change_cursor.current_cursor(store)
    with SessionDB(db_path=store) as db:
        db.end_session("s00001", "compression")
        db.create_session("tip", "desktop", parent_session_id="s00001")
        db.create_session("delegate", "desktop", parent_session_id="s00002",
                          model_config={"_delegate_from": "s00002"})
    server._session_db_content_sig(store)
    assert session_change_cursor.changes_since(store, cursor)[0] == {"s00001", "tip"}
    cursor = session_change_cursor.current_cursor(store)
    with SessionDB(db_path=store) as db:
        db.delete_session("tip")
        db.set_session_archived("s00003", True)
    server._session_db_content_sig(store)
    assert session_change_cursor.changes_since(store, cursor)[0] == {"tip", "s00001", "s00003"}


def test_retention_gap_repairs_from_full_rows_instead_of_losing_edits(store):
    server._session_db_content_sig(store)
    cursor = session_change_cursor.current_cursor(store)
    with sqlite3.connect(store) as writer:
        writer.executemany("UPDATE sessions SET message_count=? WHERE id='s00001'", [(i,) for i in range(4200)])
        writer.execute("UPDATE sessions SET title='last edit' WHERE id='s02999'")
    server._session_db_content_sig(store)
    assert session_change_cursor.changes_since(store, cursor)[0] == {"s00001", "s02999"}
