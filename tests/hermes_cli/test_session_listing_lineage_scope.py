"""A list request shares lineage work, never a later request/profile/writer cut."""
import json

from hermes_state import SessionDB
import hermes_state_local as local
from hermes_cli.web_routers import sessions


def reset_to(db, previous, new, lineage):
    db.end_session(previous, end_reason='session_reset')
    db.create_session(new, 'desktop', parent_session_id=previous, model_config={'_reset_from': previous})
    db._write_sql('INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)', (
        local.POLICY_PREFIX + 'owner', json.dumps({'session_id': 'owner', 'lineage': lineage}),
    ))


def count_indexes(monkeypatch):
    original = local.local_lineage_index
    calls = []
    def counted(conn):
        calls.append(conn)
        return original(conn)
    monkeypatch.setattr(local, 'local_lineage_index', counted)
    return calls


def test_scope_invalidates_on_other_connection_commit_and_expires(monkeypatch, tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(db_path=path) as writer:
        writer.create_session('owner', 'desktop')
        reset_to(writer, 'owner', 'next', ['owner', 'next'])
        calls = count_indexes(monkeypatch)
        with SessionDB(db_path=path, read_only=True) as reader:
            with local.listing_lineage_scope(reader):
                assert [r['id'] for r in reader.list_sessions_rich()] == ['next']
                assert reader.session_count(exclude_children=True) == 1
                assert reader.session_count(exclude_children=True, archived_only=True) == 0
                assert len(calls) == 1
                reset_to(writer, 'next', 'newest', ['owner', 'next', 'newest'])
                assert [r['id'] for r in reader.list_sessions_rich()] == ['newest']
                assert reader.session_count(exclude_children=True) == 1
                assert len(calls) == 2
            assert reader.session_count(exclude_children=True) == 1
            assert len(calls) == 3
            with local.listing_lineage_scope(reader):
                assert reader.session_count(exclude_children=True) == 1
            assert len(calls) == 4


def test_scope_does_not_share_same_ids_between_profile_readers(monkeypatch, tmp_path):
    a, b = tmp_path / 'a.db', tmp_path / 'b.db'
    with SessionDB(db_path=a) as db:
        db.create_session('owner', 'desktop')
        reset_to(db, 'owner', 'next', ['owner', 'next'])
    with SessionDB(db_path=b) as db:
        db.create_session('owner', 'desktop')
    calls = count_indexes(monkeypatch)
    with SessionDB(db_path=a, read_only=True) as one, SessionDB(db_path=b, read_only=True) as two:
        with local.listing_lineage_scope(one):
            assert [r['id'] for r in one.list_sessions_rich()] == ['next']
            with local.listing_lineage_scope(two):
                assert [r['id'] for r in two.list_sessions_rich()] == ['owner']
            assert [r['id'] for r in one.list_sessions_rich()] == ['next']
            assert len(calls) == 2


def test_route_uses_one_index_and_closes_every_request(monkeypatch, tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(db_path=path) as db:
        db.create_session('owner', 'desktop')
        reset_to(db, 'owner', 'next', ['owner', 'next'])
    calls = count_indexes(monkeypatch)
    opened = []
    def opener(profile, read_only):
        assert profile == 'ops' and read_only
        db = SessionDB(db_path=path, read_only=True)
        opened.append(db)
        return db
    monkeypatch.setattr(sessions, '_open_session_db_for_profile', opener)
    monkeypatch.setattr(sessions, '_cron_profile_home', lambda profile: ('ops', tmp_path))
    for _ in range(2):
        result = sessions.get_sessions(
            limit=100, offset=0, min_messages=0, archived='exclude', order='recent',
            source=None, sources=None, exclude_sources=None, cwd_prefix=None,
            full=False, profile='ops', changed_since=None, change_cursor=False,
        )
        assert [row['id'] for row in result['sessions']] == ['next']
        assert result['total'] == 1 and result['archived_count'] == 0
        assert opened[-1]._conn is None
    assert len(calls) == 2
