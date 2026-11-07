"""Bounded, transactional session projection journal shared by independent readers.

Only ids and sequence numbers are durable. Triggers also cover predecessor writers and
raw SQL; readers that fall behind retention take a full snapshot instead of losing edits.
"""

from __future__ import annotations

# Keep the producer and watcher on exactly the same projection contract. Activity
# heartbeats are deliberately absent; real transcript appends change message_count.
SESSION_SIGNATURE_FIELDS = (
    "id", "source", "session_key", "display_name", "model", "parent_session_id",
    "started_at", "ended_at", "end_reason", "message_count", "tool_call_count",
    "cwd", "git_branch", "git_repo_root", "title", "title_source", "profile_name",
    "archived", "pinned", "hidden", "last_read_at", "handoff_state",
)
RETAIN_CHANGES = 4096
TABLE = "session_projection_changes"
JOURNAL_SCHEMA_SQL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL
);
"""
TRIGGERS = tuple(f"session_projection_{action}" for action in ("insert", "update", "delete"))


def _trigger_definitions() -> dict[str, str]:
    definitions = {}
    for action, ref in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
        event = action.upper()
        when = ""
        if action == "update":
            event += " OF " + ", ".join(SESSION_SIGNATURE_FIELDS)
            when = " WHEN " + " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in SESSION_SIGNATURE_FIELDS)
        # An id rename is a deletion plus an insertion, even though ordinary writers
        # treat ids as immutable. Keep direct SQL and legacy stores correct too.
        old_id = (f"INSERT INTO {TABLE}(session_id) SELECT OLD.id WHERE OLD.id IS NOT NEW.id;"
                  if action == "update" else "")
        name = f"session_projection_{action}"
        definitions[name] = (f"CREATE TRIGGER IF NOT EXISTS {name} "
                             f"AFTER {event} ON sessions{when} BEGIN "
                             f"{old_id} INSERT INTO {TABLE}(session_id) VALUES ({ref}.id); "
                             f"DELETE FROM {TABLE} WHERE seq <= (SELECT MAX(seq) FROM {TABLE}) - {RETAIN_CHANGES}; END;")
    return definitions


_TRIGGER_DEFINITIONS = _trigger_definitions()


def ensure_session_change_journal(cursor) -> bool:
    """Install the optional optimization after column reconciliation, without backfill.

    AUTOINCREMENT prevents sequence reuse after pruning. No FK or custom SQL
    functions: predecessor runtimes can keep writing/deleting/pruning. Keep this
    OUT of SCHEMA_SQL: read-only browsers must not demand a store migration.
    """
    from hermes_state_schema import SessionSchemaMixin
    available = {row[1] for row in cursor.execute("PRAGMA table_info(sessions)")}
    if not set(SESSION_SIGNATURE_FIELDS) <= available:
        return False  # incomplete legacy reconciliation: remain on full snapshots
    ddl = JOURNAL_SCHEMA_SQL + "\n".join(
        f"DROP TRIGGER IF EXISTS {name};\n{sql}" for name, sql in _TRIGGER_DEFINITIONS.items())
    cursor.execute("SAVEPOINT session_projection_install")
    try:
        SessionSchemaMixin._execute_ddl_skipping_settled_triggers(cursor, ddl)
        cursor.execute("RELEASE SAVEPOINT session_projection_install")
    except BaseException:
        cursor.execute("ROLLBACK TO SAVEPOINT session_projection_install")
        cursor.execute("RELEASE SAVEPOINT session_projection_install")
        raise
    return True


def journal_available(conn) -> bool:
    """Trust only this producer contract; missing, older or newer triggers use full snapshots."""
    stored = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name IN (?, ?, ?, ?)", (TABLE, *TRIGGERS)))
    return TABLE in stored and all(
        stored.get(name) == sql.replace("IF NOT EXISTS ", "", 1).rstrip(";")
        for name, sql in _TRIGGER_DEFINITIONS.items())


def journal_window(conn) -> tuple[int, int]:
    low = conn.execute(f"SELECT seq FROM {TABLE} ORDER BY seq LIMIT 1").fetchone()
    high = conn.execute(f"SELECT seq FROM {TABLE} ORDER BY seq DESC LIMIT 1").fetchone()
    return (low[0], high[0]) if low is not None else (0, 0)


def changed_ids(conn, after: int) -> set[str]:
    return {row[0] for row in conn.execute(
        f"SELECT session_id FROM {TABLE} WHERE seq > ?", (after,))}
