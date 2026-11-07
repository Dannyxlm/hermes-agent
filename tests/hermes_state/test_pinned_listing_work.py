"""Pin backfill must not decode every unselected session's JSON configuration."""
from contextlib import closing
import sqlite3

from hermes_state import SessionDB


def test_pin_backfill_only_decodes_selected_configurations(tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(db_path=path) as db:
        db.create_session('pin', 'desktop')
        db.set_session_pinned('pin', True)
        db._execute_write(lambda conn: conn.executemany(
            'INSERT INTO sessions(id,source,started_at,model_config) VALUES(?,?,?,?)',
            [(f'unpinned-{i}', 'desktop', i, '{"model":"fixture"}') for i in range(256)]))
    with SessionDB(db_path=path, read_only=True) as db, closing(sqlite3.connect(':memory:')) as validator:
        decoded = []

        def json_valid(value):
            decoded.append(value)
            return validator.execute('SELECT json_valid(?)', (value,)).fetchone()[0]

        db._conn.create_function('json_valid', 1, json_valid, deterministic=True)
        # An empty page isolates the explicit keep/backfill path. It still obeys
        # all ordinary source, child and visibility filters before surfacing pins.
        rows = db.list_sessions_rich(limit=0, compact_rows=True, include_pinned=True)
        assert [row['id'] for row in rows] == ['pin']
        assert len(decoded) < 10
