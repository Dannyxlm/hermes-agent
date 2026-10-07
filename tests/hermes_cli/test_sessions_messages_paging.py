"""Characterize canonical history pages before replacing full-lineage payload reads."""
import asyncio
import json
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_cli.web_routers import sessions


@pytest.fixture
def lineage(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        for sid, parent in [('root', None), ('middle', 'root'), ('tip', 'middle')]:
            db.create_session(sid, 'desktop', parent_session_id=parent)
        expected = []
        for n in range(2000):
            sid = ['root', 'middle', 'tip'][min(n // 700, 2)]
            row_id = db.append_message(sid, 'assistant', f'row-{n}', timestamp=2000-n)
            expected.append((row_id, f'row-{n}'))
        # A copied protected tail wins representation but retains first-row order.
        db._conn.execute("UPDATE messages SET active=0, compacted=1 WHERE id=?", (expected[699][0],))
        clone = db.append_message('middle', 'assistant', 'row-699', timestamp=1301)
        expected[699] = (clone, 'row-699')
        db.create_session('sibling', 'desktop', parent_session_id='root', model_config={'_branched_from': 'root'})
        db.append_message('sibling', 'assistant', 'must-not-leak')
        db._conn.execute("UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id IN ('root', 'middle')")
        yield db, expected


@pytest.fixture
def legacy_lineage(tmp_path):
    path = tmp_path / 'legacy.db'
    with SessionDB(db_path=path) as db:
        db.create_session('root', 'desktop')
        db.append_message('root', 'user', 'old', timestamp=10)
        db._write_sql('UPDATE messages SET active=0, compacted=1')
        db.create_session('tip', 'desktop', parent_session_id='root')
        copied = db.append_message('tip', 'user', 'old', timestamp=10)
        newest = db.append_message('tip', 'assistant', 'new', timestamp=11)
        db._write_sql("UPDATE sessions SET end_reason='compression', ended_at=12 WHERE id='root'")
    with sqlite3.connect(path) as conn:
        dependent = conn.execute("SELECT type,name FROM sqlite_master WHERE type IN ('trigger','index') "
                                 "AND (sql LIKE '%display_identity%' OR sql LIKE '%display_order%')").fetchall()
        for kind, name in dependent:
            conn.execute(f'DROP {kind} "{name}"')
        for column in ('display_identity', 'display_order'):
            conn.execute(f'ALTER TABLE messages DROP COLUMN {column}')
    with SessionDB(db_path=path, read_only=True) as db:
        yield db, [(copied, 'old'), (newest, 'new')]
        assert 'display_identity' not in {row[1] for row in db._conn.execute('PRAGMA table_info(messages)')}


def test_readonly_legacy_lineage_pages_use_computed_identity(legacy_lineage):
    db, expected = legacy_lineage
    rows = db.get_messages('tip', include_ancestors=True, include_compacted=True, limit=50)
    assert [(row['id'], row['content']) for row in rows] == expected
    latest = db.get_messages('tip', include_ancestors=True, include_compacted=True, latest=True, limit=1)
    assert [(row['id'], row['content']) for row in latest] == expected[-1:]


@pytest.mark.parametrize('size', [50, 100, 500])
@pytest.mark.parametrize('offset', [0, 100, 650])
@pytest.mark.parametrize('latest', [False, True])
def test_long_lineage_exact_pages(lineage, size, offset, latest):
    db, expected = lineage
    rows = db.get_messages('tip', include_ancestors=True, include_compacted=True,
                           limit=size, offset=offset, latest=latest)
    page = expected[::-1][offset:][:size][::-1] if latest else expected[offset:][:size]
    assert [(row['id'], row['content']) for row in rows] == page
    assert db.get_messages('sibling', include_ancestors=True)[0]['content'] == 'must-not-leak'


def test_rest_page_and_default_detail(lineage, monkeypatch, tmp_path):
    db, expected = lineage
    monkeypatch.setattr(sessions, '_with_db', lambda profile, fn, **kw: fn(db))
    monkeypatch.setattr(sessions, '_history_profile_home', lambda profile: tmp_path)
    page = asyncio.run(sessions.get_session_messages('tip', limit=100, offset=100, order='latest',
                           include_compacted=True, inline_images=False, display_only=False))
    assert [(row['id'], row['content']) for row in page['messages']] == expected[-200:-100]
    detail = asyncio.run(sessions.get_session_detail('tip'))
    original = db.get_session('tip')
    original.update(profile='default', is_default_profile=True)
    assert json.dumps(detail) == json.dumps(original)


def test_compact_detail_omits_blobs_and_preserves_metadata(lineage, monkeypatch):
    db, _ = lineage
    db._conn.execute("UPDATE sessions SET system_prompt=?, model_config=?, tool_names=?, cwd=?, model=? WHERE id='tip'",
                     ('large prompt', '{"max_tokens":123}', '["terminal"]', '/fixture', 'fixture-model'))
    monkeypatch.setattr(sessions, '_with_db', lambda profile, fn, **kw: fn(db))
    full = asyncio.run(sessions.get_session_detail('tip'))
    compact = asyncio.run(sessions.get_session_detail('tip', compact=True))
    assert compact == {k: v for k, v in full.items() if k not in {'system_prompt', 'model_config', 'tool_names'}}
    assert compact['cwd'] == '/fixture'
    assert compact['model'] == 'fixture-model'
    assert compact['parent_session_id'] == 'middle'
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(sessions.manage_router)
    client = TestClient(app)
    assert client.get('/api/sessions/tip').content == client.get('/api/sessions/tip?compact=0').content
    assert client.get('/api/sessions/tip?compact=1').json() == compact


def test_lineage_only_materializes_page_payloads(lineage, monkeypatch):
    from contextlib import contextmanager
    db, expected = lineage
    payloads = []
    read_ctx = db._read_ctx
    @contextmanager
    def counted_read():
        with read_ctx() as conn:
            factory = conn.row_factory
            def count_payloads(cursor, row):
                if 'content' in [col[0] for col in cursor.description]:
                    payloads.append(1)
                return factory(cursor, row)
            conn.row_factory = count_payloads
            try:
                yield conn
            finally:
                conn.row_factory = factory
    monkeypatch.setattr(db, '_read_ctx', counted_read)
    rows = db.get_messages('tip', include_ancestors=True, include_compacted=True, latest=True, limit=100)
    assert [(m['id'], m['content']) for m in rows] == expected[-100:]
    assert len(payloads) == 100
