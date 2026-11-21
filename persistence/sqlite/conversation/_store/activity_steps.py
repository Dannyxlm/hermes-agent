"""Read activity labels across legacy-column and side-table stores."""


def _has_step_columns(conn):
    return {'last_activity_kind', 'last_activity_tool'} <= {
        row[1] for row in conn.execute('PRAGMA table_info(sessions)')}


def write_step(conn, session_id, kind, tool):
    if _has_step_columns(conn):
        conn.execute('UPDATE sessions SET last_activity_kind=?,last_activity_tool=? WHERE id=?',
                     (kind, tool, session_id))
        # A new write (including a clear) supersedes any side-table-only label.
        conn.execute('DELETE FROM session_activity_steps WHERE session_id=?', (session_id,))
    else:
        conn.execute('INSERT INTO session_activity_steps(session_id,kind,tool) VALUES (?,?,?) '
                     'ON CONFLICT(session_id) DO UPDATE SET kind=excluded.kind,tool=excluded.tool',
                     (session_id, kind, tool))


def read_step(conn, session_id, row):
    legacy = row.get('last_activity_kind'), row.get('last_activity_tool')
    # The rollback reader may have added two NULL columns to a side-table-only store.
    # A populated legacy step is authoritative as a pair: a NULL tool may be intentional.
    if legacy != (None, None):
        return legacy
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_activity_steps'").fetchone():
        return None, None
    step = conn.execute('SELECT kind,tool FROM session_activity_steps WHERE session_id=?', (session_id,)).fetchone()
    return tuple(step) if step else (None, None)
