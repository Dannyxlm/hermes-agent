"""Optional listing accelerators. Install only after draining the profile offline."""
from pathlib import Path
import sqlite3


LISTING_INDEX_SQL = (
    """CREATE INDEX IF NOT EXISTS idx_state_meta_local_lineage
    ON state_meta(key)
    WHERE key GLOB 'gateway.local_policy.v1:*' AND json_valid(value)
      AND json_array_length(value, '$.lineage') > 1""",
    """CREATE INDEX IF NOT EXISTS idx_sessions_listing_scope
    ON sessions(archived, source, hidden, message_count, parent_session_id, model_config,
                session_key, created_source, cwd, id, started_at, last_activity_at)""",
    """CREATE INDEX IF NOT EXISTS idx_sessions_listing_pinned
    ON sessions(pinned, started_at DESC) WHERE pinned = 1""",
)


def install_listing_indexes(home: Path) -> None:
    """Refuse runtime ownership and writer contention rather than delaying a chat turn.

    Never called by schema initialization or listing. The operator must also drain
    standalone clients before this explicit maintenance operation.
    """
    from gateway.runtime_ownership import exclusive_maintenance

    home = home.expanduser().absolute()
    path = (home / 'state.db').resolve()
    with exclusive_maintenance([home, path.parent], purpose='listing-index-install'):
        conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            for statement in LISTING_INDEX_SQL:
                expected = statement.replace('IF NOT EXISTS ', '')
                name = expected.split()[2]
                existing = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                if existing and ' '.join(existing[0].split()) != ' '.join(expected.split()):
                    raise ValueError(f'Unexpected definition for {name}; no indexes changed')
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path, help='Drained, offline profile home')
    install_listing_indexes(parser.parse_args().home)
