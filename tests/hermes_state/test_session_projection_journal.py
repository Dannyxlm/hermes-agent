"""The session projection journal is optional for reads and upgradeable by ordinary writers."""

import sqlite3

from hermes_state import SessionDB
from hermes_state_reconcile import schema_drift_reason
from hermes_state_session_changes import TRIGGERS, ensure_session_change_journal, journal_available


def test_legacy_read_only_browsing_does_not_require_the_journal(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("legacy", "desktop")
        db.append_message("legacy", "user", "synthetic")
        for name in TRIGGERS:
            db._conn.execute(f"DROP TRIGGER {name}")
        db._conn.execute("DROP TABLE session_projection_changes")
    assert schema_drift_reason(path) is None
    with SessionDB(db_path=path, read_only=True) as db:
        assert len(db.get_messages("legacy")) == 1
        assert not journal_available(db._conn)
    with SessionDB(db_path=path) as db:
        assert journal_available(db._conn)
        assert len(db.get_messages("legacy")) == 1


def test_writer_replaces_obsolete_trigger_bodies_without_recreating_settled_ones(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("chat", "desktop")
        db._conn.execute("DROP TRIGGER session_projection_update")
        db._conn.execute("""CREATE TRIGGER session_projection_update AFTER UPDATE OF message_count ON sessions
            BEGIN INSERT INTO session_projection_changes(session_id) VALUES(NEW.id); END""")
        assert not journal_available(db._conn)
    with SessionDB(db_path=path) as db:
        assert journal_available(db._conn)
        high = db._conn.execute("SELECT MAX(seq) FROM session_projection_changes").fetchone()[0]
        db.set_session_title("chat", "changed")
        assert db._conn.execute("SELECT MAX(seq) FROM session_projection_changes").fetchone()[0] > high
        cookie = db._conn.execute("PRAGMA schema_version").fetchone()[0]
        ensure_session_change_journal(db._conn.cursor())
        assert db._conn.execute("PRAGMA schema_version").fetchone()[0] == cookie


def test_incomplete_legacy_schema_can_skip_this_optional_optimization():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        assert not ensure_session_change_journal(conn.cursor())
        assert not journal_available(conn)
        conn.execute("INSERT INTO sessions(id) VALUES('legacy')")
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
