"""Membership reads stay bounded, indexed and strictly read-only even on damaged lineage/schema."""
import sqlite3
from contextlib import closing

import pytest

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_holder import holder_type, raw_holder
from tests.fixtures.storage_seed import seed_messages, seed_session
from tests.hermes_cli.test_session_list_membership import launch, membership

pytest_plugins = ['tests.hermes_cli.test_profiles_sidebar_scope']


@pytest.mark.parametrize('corruption', ['cycle', 'depth'])
def test_invalid_lineage_is_bounded_without_poisoning_other_ids(client, profiles_on_disk, monkeypatch, corruption):
    from persistence.sqlite.conversation._store import compression
    with open_persistence(profile_scope(home=profiles_on_disk['default'])) as db:
        seed_session(db, 'root', 'desktop')
        launch(db, 'root')
        db.sessions.end('root', 'compression')
        seed_session(db, 'middle', 'desktop', parent_session_id='root')
        db.sessions.end('middle', 'compression')
        seed_session(db, 'tip', 'desktop', parent_session_id='middle')
        seed_session(db, 'good', 'desktop')
        launch(db, 'good')
        if corruption == 'cycle':
            raw_holder(db)._write_sql("UPDATE sessions SET parent_session_id='tip' WHERE id='root'")
            db.sessions.end('tip', 'compression')
        else:
            monkeypatch.setattr(compression, '_CHAIN_CAP', 2)
    response = membership(client, ['tip', 'good'])
    assert response.status_code == 200, response.text
    assert response.json()['thread_session_ids'] == ['good']
    assert client.get('/api/sessions/tip').json()['thread_parent_id'] is None
    assert client.get('/api/sessions/good').json()['thread_parent_id'] == 'opener'


def test_membership_never_initializes_or_reconciles_schema(client, profiles_on_disk, monkeypatch):
    home = profiles_on_disk['default']
    response = membership(client, ['missing'])
    assert response.status_code == 503, response.text
    assert not (home / 'state.db').exists()
    with open_persistence(profile_scope(home=home)) as db:
        seed_session(db, 'child', 'desktop')
        launch(db, 'child')
        raw_holder(db)._write_sql('DROP TABLE work_launches')
    with closing(sqlite3.connect(home / 'state.db')) as conn:
        before = conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
    def no_writer(self):
        pytest.fail('membership attempted a writable store open')
    monkeypatch.setattr(holder_type(), '_open_writer', no_writer)
    response = membership(client, ['child'])
    assert response.status_code == 503, response.text
    assert response.json() == {'detail': 'Session store unavailable; membership is unknown'}
    with closing(sqlite3.connect(home / 'state.db')) as conn:
        assert conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall() == before


def test_batch_probes_use_indexes_and_never_read_transcripts_or_write(client, profiles_on_disk, monkeypatch):
    home = profiles_on_disk['default']
    with open_persistence(profile_scope(home=home)) as db:
        seed_session(db, 'child', 'desktop')
        launch(db, 'child')
        db.sessions.end('child', 'compression')
        seed_session(db, 'tip', 'desktop', parent_session_id='child')
        seed_messages(db, 'tip', {'role': 'user', 'content': 'private transcript'})
    plans = []
    def inspect_read(self, sql, params=()):
        with self._read_ctx() as conn:
            def authorize(action, table, column, *_):
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == 'messages' and column not in {'session_id', 'timestamp'}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            try:
                plan = conn.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()
                plans.append([row['detail'] for row in plan])
                result = conn.execute(sql, params).fetchall()
                return result
            finally:
                conn.set_authorizer(None)
    monkeypatch.setattr(holder_type(), '_read_all', inspect_read)
    response = membership(client, ['tip', *[f'absent-{i}' for i in range(99)]])
    assert response.status_code == 200, response.text
    assert response.json()['thread_session_ids'] == ['tip']
    assert len(plans) == 2
    assert any('idx_sessions_parent' in step for step in plans[0]), plans
    assert any('idx_work_launches_child' in step for step in plans[1]), plans
    print('Membership query plans:', plans)


def test_unknown_profile_is_not_an_all_profile_scan(client):
    response = membership(client, ['child'], 'absent-profile')
    assert response.status_code == 404, response.text


def test_existing_provenance_trim_semantics_are_preserved(client, profiles_on_disk):
    # SQL trim() in thread_parents/exclude_threads_where removes spaces, not tabs.
    # This read-only seam must not redefine the underlying provenance predicate.
    with open_persistence(profile_scope(home=profiles_on_disk['default'])) as db:
        seed_session(db, 'legacy-tab-launcher', 'desktop')
        launch(db, 'legacy-tab-launcher', launcher='\t')
    response = membership(client, ['legacy-tab-launcher'])
    assert response.status_code == 200, response.text
    assert response.json()['thread_session_ids'] == ['legacy-tab-launcher']
    assert client.get('/api/sessions/legacy-tab-launcher').json()['thread_parent_id'] == '\t'
