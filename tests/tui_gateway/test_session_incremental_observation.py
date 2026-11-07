"""Session observations must cost proportional to committed projection changes, not history."""

import sqlite3

import pytest

from hermes_state import SessionDB
from tui_gateway import server, session_change_cursor


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db._conn.execute("BEGIN IMMEDIATE")
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


def test_same_count_projection_edits_are_all_observed(store):
    server._session_db_content_sig(store)
    edits = {"source": "mobile", "session_key": "changed", "display_name": "changed",
             "model": "changed", "parent_session_id": "s00002", "started_at": 2,
             "ended_at": 3, "end_reason": "compression", "tool_call_count": 2,
             "cwd": "/changed", "git_branch": "changed", "git_repo_root": "/changed",
             "title": "changed", "title_source": "user", "profile_name": "changed",
             "archived": 1, "pinned": 1, "hidden": 1, "last_read_at": 10, "handoff_state": "changed"}
    with sqlite3.connect(store) as writer:
        for field, value in edits.items():
            cursor = session_change_cursor.current_cursor(store)
            writer.execute(f"UPDATE sessions SET {field}=? WHERE id='s00001'", (value,))
            writer.commit()
            server._session_db_content_sig(store)
            assert session_change_cursor.changes_since(store, cursor)[0] == {"s00001"}, field


def test_racing_commit_is_not_consumed_by_an_older_snapshot(store, monkeypatch):
    from tui_gateway import session_observation
    server._session_db_content_sig(store)
    with sqlite3.connect(store) as writer:
        writer.execute("UPDATE sessions SET title='first' WHERE id='s00001'")
    original = session_observation._read_rows

    def racing_read(*args):
        rows = original(*args)
        with sqlite3.connect(store) as writer:
            writer.execute("UPDATE sessions SET title='racing' WHERE id='s00001'")
        return rows

    monkeypatch.setattr(session_observation, "_read_rows", racing_read)
    server._session_db_content_sig(store)
    cursor = session_change_cursor.current_cursor(store)
    monkeypatch.setattr(session_observation, "_read_rows", original)
    server._session_db_content_sig(store)
    assert session_change_cursor.changes_since(store, cursor)[0] == {"s00001"}


def test_rolled_back_and_noop_writes_do_not_advance_the_cursor(store):
    before = server._session_db_content_sig(store)
    cursor = session_change_cursor.current_cursor(store)
    with sqlite3.connect(store) as writer:
        writer.execute("UPDATE sessions SET title='uncommitted' WHERE id='s00001'")
        writer.rollback()
        writer.execute("UPDATE sessions SET title=title WHERE id='s00001'")
    assert server._session_db_content_sig(store) == before
    assert session_change_cursor.changes_since(store, cursor) == (set(), cursor)


def test_missing_trigger_uses_full_fallback_until_writer_repairs_it(store):
    server._session_db_content_sig(store)
    cursor = session_change_cursor.current_cursor(store)
    with sqlite3.connect(store) as writer:
        writer.execute("DROP TRIGGER session_projection_update")
        writer.execute("UPDATE sessions SET title='fallback' WHERE id='s00001'")
    server._session_db_content_sig(store)
    assert session_change_cursor.changes_since(store, cursor)[0] == {"s00001"}
    with SessionDB(db_path=store) as db:
        db.set_session_title("s00002", "repaired")
    cursor = session_change_cursor.current_cursor(store)
    server._session_db_content_sig(store)
    assert session_change_cursor.changes_since(store, cursor)[0] == {"s00002"}


def test_replacement_identity_race_never_seeds_a_partial_cursor_scope(store, monkeypatch):
    server._session_db_content_sig(store)
    original_cursor = session_change_cursor.current_cursor(store)
    changes = []
    monkeypatch.setattr(session_change_cursor, "_listeners", [changes.append])
    with sqlite3.connect(store) as writer:
        writer.execute("UPDATE sessions SET title='changed' WHERE id='s00001'")
    identity = session_change_cursor.stat_db_file_identity
    # The probe still owns its old-file snapshot, while the cursor's later stat
    # sees replacement. A delta is not a valid baseline for that new identity.
    monkeypatch.setattr(session_change_cursor, "stat_db_file_identity", lambda _key: (-1, -1))
    server._session_db_content_sig(store)
    assert session_change_cursor.current_cursor(store) is None
    monkeypatch.setattr(session_change_cursor, "stat_db_file_identity", identity)
    server._session_db_content_sig(store)
    assert session_change_cursor.changes_since(store, original_cursor)[0] is None
    assert changes == []  # full re-seed is repair, not thousands of new-reply candidates


def test_id_rename_emits_old_tombstone_and_new_row(store):
    server._session_db_content_sig(store)
    cursor = session_change_cursor.current_cursor(store)
    with sqlite3.connect(store) as writer:
        writer.execute("UPDATE sessions SET id='renamed' WHERE id='s00001'")
    server._session_db_content_sig(store)
    assert session_change_cursor.changes_since(store, cursor)[0] == {"s00001", "renamed"}
