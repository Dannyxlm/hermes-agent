"""Listing accelerators never install during a user's browse or chat open."""
import sqlite3

import pytest

from hermes_state import SessionDB

INDEXES = {'idx_state_meta_local_lineage', 'idx_sessions_listing_scope', 'idx_sessions_listing_pinned'}


def index_names(path):
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


@pytest.mark.parametrize('reopen', [False, True])
def test_optional_listing_indexes_are_not_installed_by_writable_open(tmp_path, reopen):
    path = tmp_path / 'state.db'
    with SessionDB(db_path=path) as db:
        db.create_session('chat', 'mobile')
        db.append_message('chat', 'user', 'hello')
    if reopen:
        with SessionDB(db_path=path) as db:
            db.append_message('chat', 'assistant', 'world')
    with SessionDB(db_path=path, read_only=True) as db:
        assert db.list_sessions_rich()[0]['preview'] == 'hello'
    assert not INDEXES.intersection(index_names(path)), 'opening or browsing must not build optional indexes'


def test_offline_install_is_idempotent_and_preserves_fallback_rows(tmp_path):
    from hermes_state_listing_indexes import install_listing_indexes

    path = tmp_path / 'state.db'
    with SessionDB(db_path=path) as db:
        db.create_session('chat', 'mobile')
        db.append_message('chat', 'user', 'hello')
        before = db.list_sessions_rich()
    install_listing_indexes(tmp_path)
    assert INDEXES.issubset(index_names(path))
    install_listing_indexes(tmp_path)
    with SessionDB(db_path=path, read_only=True) as db:
        assert db.list_sessions_rich() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA quick_check').fetchall() == [('ok',)]


def test_offline_install_refuses_owned_profile_before_opening_database(tmp_path):
    from gateway.runtime_ownership import OwnershipConflict, ProfileOwnership
    from hermes_state_listing_indexes import install_listing_indexes

    owner = ProfileOwnership()
    owner.reserve([tmp_path])
    try:
        with pytest.raises(OwnershipConflict):
            install_listing_indexes(tmp_path)
        assert not (tmp_path / 'state.db').exists()
    finally:
        owner.release(tmp_path)


def test_offline_install_refuses_writer_and_rolls_back_partial_ddl(tmp_path, monkeypatch):
    import hermes_state_listing_indexes as indexes

    path = tmp_path / 'state.db'
    with SessionDB(db_path=path):
        pass
    with sqlite3.connect(path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            indexes.install_listing_indexes(tmp_path)
    assert not INDEXES.intersection(index_names(path))
    monkeypatch.setattr(indexes, 'LISTING_INDEX_SQL', (*indexes.LISTING_INDEX_SQL, 'CREATE INDEX bad ON missing_table(x)'))
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        indexes.install_listing_indexes(tmp_path)
    assert not INDEXES.intersection(index_names(path))


def test_offline_install_does_not_create_missing_store(tmp_path):
    from hermes_state_listing_indexes import install_listing_indexes

    with pytest.raises(sqlite3.OperationalError, match='unable to open'):
        install_listing_indexes(tmp_path)
    assert not (tmp_path / 'state.db').exists()


def test_offline_install_refuses_unknown_same_name_index_without_rebuilding(tmp_path):
    from hermes_state_listing_indexes import install_listing_indexes

    path = tmp_path / 'state.db'
    with SessionDB(db_path=path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE INDEX idx_sessions_listing_scope ON sessions(id)')
    with pytest.raises(ValueError, match='Unexpected definition'):
        install_listing_indexes(tmp_path)
    assert INDEXES.intersection(index_names(path)) == {'idx_sessions_listing_scope'}
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA index_info('idx_sessions_listing_scope')").fetchall() == [(0, 0, 'id')]
