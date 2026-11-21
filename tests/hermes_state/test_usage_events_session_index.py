"""Usage cascade lookup is indexed, including stores created before the index existed."""
from contextlib import closing

import sqlite3

from hermes_state import SessionDB


def test_existing_store_installs_usage_session_index_idempotently(tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(path) as db:
        db.create_session('kept', 'cron')
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('DROP INDEX IF EXISTS idx_usage_events_session')
    for _ in range(2):
        with SessionDB(path) as db:
            with db._read_ctx() as conn:
                assert [r[2] for r in conn.execute('PRAGMA index_info(idx_usage_events_session)')] == ['session_id']
                plan = conn.execute('EXPLAIN QUERY PLAN SELECT * FROM usage_events WHERE session_id=?', ('kept',)).fetchall()
                assert any('idx_usage_events_session' in row[3] for row in plan)
            assert db.get_session('kept') is not None


def test_usage_events_cascade_only_deleted_session(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        for sid in ['gone', 'kept']:
            db.create_session(sid, 'cron')
            db._write_sql('''INSERT INTO usage_events(session_id,chat_id,occurred_at,model,provider,
                input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,api_call_count,pricing_date)
                VALUES(?,?,1,'m','p',1,1,0,0,0,1,'2026-01-01')''', (sid, sid))
        assert db.delete_session('gone')
        assert [r[0] for r in db._read_all('SELECT session_id FROM usage_events')] == ['kept']
