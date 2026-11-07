"""Cold lists skip unrelated receipt blobs and reflect committed lineage rewrites."""
import json
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, listing_lineage_scope


def receipt(owner, lineage, padding=''):
    return json.dumps({'session_id': owner, 'lineage': lineage, 'policy': padding})


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(db_path=path) as db:
        for sid in ('owner', 'first', 'other'):
            db.create_session(sid, 'desktop')
        db._write_sql("UPDATE sessions SET end_reason='session_reset' WHERE id='owner'")
        db._write_sql('INSERT INTO state_meta VALUES(?,?)', (
            POLICY_PREFIX + 'owner', receipt('owner', ['owner', 'first'])))
        from hermes_state_listing_indexes import LISTING_INDEX_SQL
        with db._conn:
            for statement in LISTING_INDEX_SQL:
                db._conn.execute(statement)
            db._conn.executemany('INSERT INTO state_meta VALUES(?,?)', [
                (POLICY_PREFIX + f'filler-{i:04}', receipt(f'filler-{i}', [f'filler-{i}'], 'x' * 43000))
                for i in range(300)])
    return path


def test_cold_page_does_not_parse_unrelated_creation_receipts(store):
    parsed = []
    with SessionDB(db_path=store, read_only=True) as db:
        with db._read_ctx() as conn:
            def array_length(raw, path):
                parsed.append(1)
                value = json.loads(raw).get(path.removeprefix('$.'))
                return len(value) if isinstance(value, list) else 0
            conn.create_function('json_array_length', 2, array_length, deterministic=True)
        rows = db.list_sessions_rich(limit=1, compact_rows=True, order_by_last_active=True)
        assert len(rows) == 1
        assert len(parsed) == 0, 'a small page must not parse every frozen creation policy'


def test_equal_length_receipt_rewrite_is_visible_on_next_disposable_reader(store):
    def ids():
        with SessionDB(db_path=store, read_only=True) as db, listing_lineage_scope(db):
            rows = db.list_sessions_rich(compact_rows=True)
            return {r['id']: r.get('_lineage_ids') for r in rows}
    assert ids()['first'] == ['owner', 'first']
    old, new = receipt('owner', ['owner', 'first']), receipt('owner', ['owner', 'other'])
    assert len(old) == len(new)
    with sqlite3.connect(store) as conn:
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (new, POLICY_PREFIX + 'owner'))
    rows = ids()
    assert rows['other'] == ['owner', 'other']
    assert rows['first'] is None


@pytest.mark.parametrize('index_present', [False, True])
def test_receipt_membership_matches_original_json_semantics(store, index_present):
    with sqlite3.connect(store) as conn:
        if not index_present:
            conn.execute('DROP INDEX IF EXISTS idx_state_meta_local_lineage')
        conn.executemany('INSERT INTO state_meta VALUES(?,?)', [
            (POLICY_PREFIX + 'bad', 'not json'),
            (POLICY_PREFIX + 'wrong-owner', receipt(23, ['owner', 'other'])),
            (POLICY_PREFIX + 'wrong-element', receipt('owner', ['owner', 23])),
            (POLICY_PREFIX + 'z-duplicate', '{"session_id":"wrong","session_id":"owner",'
             '"lineage":["owner","first"],"lineage":["owner","other"]}'),
        ])
    with SessionDB(db_path=store, read_only=True) as db:
        rows = {r['id']: r for r in db.list_sessions_rich(limit=20, compact_rows=True)}
    assert 'owner' not in rows
    assert rows['first']['_lineage_ids'] == ['owner', 'first']
    assert rows['other']['_lineage_ids'] == ['owner', 'other']


@pytest.mark.parametrize('mutation', ['archive', 'pin', 'rename', 'delete', 'message', 'heartbeat', 'usage', 'compression'])
def test_cold_lists_reflect_commits(store, mutation):
    with SessionDB(db_path=store) as writer:
        def listed():
            with SessionDB(db_path=store, read_only=True) as reader:
                return reader.list_sessions_rich(limit=20, compact_rows=True, order_by_last_active=True, include_pinned=True)
        assert any(r['id'] == 'other' for r in listed())
        sql = {
            'archive': ("UPDATE sessions SET archived=1 WHERE id='other'", ()),
            'pin': ("UPDATE sessions SET pinned=1,archived=1 WHERE id='other'", ()),
            'rename': ("UPDATE sessions SET title='new name' WHERE id='other'", ()),
            'delete': ("DELETE FROM sessions WHERE id='other'", ()),
            'heartbeat': ("UPDATE sessions SET last_activity_at=1794074400 WHERE id='other'", ()),
            'usage': ("UPDATE sessions SET input_tokens=1234 WHERE id='other'", ()),
        }
        if mutation in sql:
            writer._write_sql(*sql[mutation])
        elif mutation == 'message':
            writer.append_message('other', 'user', 'new message')
        else:
            writer._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='other'")
            writer.create_session('child', 'desktop', parent_session_id='other')
        rows = {r['id']: r for r in listed()}
        if mutation in ('archive', 'delete'):
            assert 'other' not in rows
        elif mutation == 'compression':
            assert rows['child']['_lineage_ids'] == ['other', 'child']
        else:
            key, expected = {
                'pin': ('pinned', 1), 'rename': ('title', 'new name'), 'message': ('preview', 'new message'),
                'heartbeat': ('last_active', 1794074400), 'usage': ('input_tokens', 1234),
            }[mutation]
            assert rows['other'][key] == expected
