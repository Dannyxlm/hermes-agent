"""Optional activity labels without changing the positional sessions row layout."""


def _has_step_columns(conn):
    return {'last_activity_kind', 'last_activity_tool'} <= {
        row[1] for row in conn.execute('PRAGMA table_info(sessions)')}


def write_step(conn, session_id, kind, tool):
    if _has_step_columns(conn):
        conn.execute('UPDATE sessions SET last_activity_kind=?,last_activity_tool=? WHERE id=?',
                     (kind, tool, session_id))
    else:
        conn.execute('INSERT INTO session_activity_steps(session_id,kind,tool) VALUES (?,?,?) '
                     'ON CONFLICT(session_id) DO UPDATE SET kind=excluded.kind,tool=excluded.tool',
                     (session_id, kind, tool))


def read_step(conn, session_id, row):
    if 'last_activity_kind' in row and 'last_activity_tool' in row:
        return row['last_activity_kind'], row['last_activity_tool']
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_activity_steps'").fetchone():
        return None, None
    step = conn.execute('SELECT kind,tool FROM session_activity_steps WHERE session_id=?', (session_id,)).fetchone()
    return tuple(step) if step else (None, None)
