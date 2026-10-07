"""Characterize canonical history pages before replacing full-lineage payload reads."""
import asyncio
import json

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
