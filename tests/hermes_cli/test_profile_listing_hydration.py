"""Only globally returned profile-list rows need transcript previews."""
import inspect
import sqlite3

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from hermes_cli.web_routers import profiles
from hermes_state import SessionDB


@pytest.fixture
def listing(tmp_path, monkeypatch):
    targets = [(name, tmp_path / name) for name in ('default', 'other')]
    for index, (name, home) in enumerate(targets):
        home.mkdir()
        (home / 'config.yaml').write_text('{}\n')
        with SessionDB(db_path=home / 'state.db') as db:
            for i in range(4):
                sid = f'{name}-{i}'
                db.create_session(sid, 'desktop')
                db.append_message(sid, 'user', f'hello {sid}', timestamp=1700000000 + i * 10 + index)
                db._write_sql('UPDATE sessions SET started_at=?,last_activity_at=? WHERE id=?',
                              (1700000000 + i * 10 + index, 1700000000 + i * 10 + index, sid))
    parsed = []
    def opener(path, *, read_only):
        assert read_only
        db = SessionDB(db_path=path, read_only=True)
        with db._read_ctx() as conn:
            conn.execute('PRAGMA query_only=1')
            def replace(text, old, new):
                if text.startswith('hello '):
                    parsed.append(1)
                if isinstance(old, bytes):
                    old = old.decode()
                return text.replace(old, new)
            conn.create_function('replace', 3, replace, deterministic=True)
        return db
    monkeypatch.setattr(profiles, '_open_session_db_at_path', opener)
    monkeypatch.setattr(profiles, '_profile_targets', lambda label: targets)
    profiles._sidebar_profile_cache_clear()
    app = FastAPI()
    app.add_api_route('/api/profiles/sessions', profiles.get_profiles_sessions)
    app.add_api_route('/api/profiles/sessions/sidebar', inspect.unwrap(profiles.get_profiles_sessions_sidebar))
    with TestClient(app) as client:
        yield client, parsed
    profiles._sidebar_profile_cache_clear()


def test_global_page_hydrates_only_returned_previews(listing):
    client, parsed = listing
    response = client.get('/api/profiles/sessions?limit=2&order=recent')
    assert response.status_code == 200
    data = response.json()
    assert [(r['id'], r['preview']) for r in data['sessions']] == [
        ('other-3', 'hello other-3'), ('default-3', 'hello default-3')]
    assert data['total'] == 8
    assert len(parsed) == 2 * len(data['sessions'])


def test_sidebar_hydrates_overlapping_windows_once(listing):
    client, parsed = listing
    response = client.get('/api/profiles/sessions/sidebar?recents_limit=2&cron_limit=2&messaging_limit=2')
    assert response.status_code == 200
    data = response.json()
    assert [r['id'] for r in data['recents']['sessions']] == ['other-3', 'default-3']
    assert data['recents']['sessions'] == data['messaging']['sessions']
    assert data['messaging']['total'] == 4
    assert len(parsed) == 2 * len(data['recents']['sessions'])


def test_failed_hydration_is_not_an_authoritative_empty_sidebar(listing, monkeypatch):
    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(SessionDB, '_get_session_rich_rows_batch', unreadable)
    client, _ = listing
    response = client.get('/api/profiles/sessions/sidebar?recents_limit=2&messaging_limit=2')
    assert response.status_code == 200
    data = response.json()
    for key in ('recents', 'cron', 'messaging'):
        assert 'sessions' not in data[key]
        assert data[key]['failed'] and data[key]['retry']
