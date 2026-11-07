"""Unindexed disposable listing readers reuse only unchanged receipt membership."""
import json
import os
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, listing_lineage_scope


@pytest.fixture
def store(tmp_path, monkeypatch):
    import hermes_state_local as local
    from collections import OrderedDict

    monkeypatch.setattr(local, '_LINEAGE_RECEIPTS', OrderedDict(), raising=False)
    path=tmp_path/'state.db'
    with SessionDB(db_path=path) as db:
        for sid in ('owner','first','other'):
            db.create_session(sid,'desktop')
        db.end_session('owner','session_reset')
        db._write_sql('INSERT INTO state_meta VALUES(?,?)',(POLICY_PREFIX+'owner',json.dumps({'session_id':'owner','lineage':['owner','first']})))
    return path


def listed(path, statements=None):
    with SessionDB(db_path=path,read_only=True) as db, listing_lineage_scope(db):
        if statements is not None:
            db._conn.set_trace_callback(statements.append)
        return {row['id']:row.get('_lineage_ids') for row in db.list_sessions_rich()}


def scans(statements):
    return [sql for sql in statements if sql.startswith('SELECT value FROM state_meta') and 'json_array_length' in sql]


def test_disposable_readers_do_not_repeat_unchanged_unindexed_receipt_scan(store):
    statements=[]
    assert listed(store,statements)['first']==['owner','first']
    assert listed(store,statements)['first']==['owner','first']
    assert len(scans(statements))==1


@pytest.mark.parametrize('wal',[False,True])
def test_equal_length_receipt_commit_invalidates_disposable_cache(store,wal):
    assert listed(store)['first']==['owner','first']
    before=store.stat()
    with sqlite3.connect(store) as writer:
        if wal:
            writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('UPDATE state_meta SET value=? WHERE key=?',(json.dumps({'session_id':'owner','lineage':['owner','other']}),POLICY_PREFIX+'owner'))
        writer.commit()
        if not wal:
            os.utime(store,ns=(before.st_atime_ns,before.st_mtime_ns))
        result=listed(store)
        assert result['other']==['owner','other']
        assert result['first'] is None


def test_end_reason_is_still_read_fresh_after_receipt_reuse(store):
    assert 'owner' not in listed(store)
    with sqlite3.connect(store) as writer:
        writer.execute("UPDATE sessions SET end_reason='compression' WHERE id='owner'")
    assert 'owner' in listed(store)


def test_snapshot_receipts_do_not_escape_into_next_disposable_reader(store):
    with SessionDB(db_path=store,read_only=True) as db, listing_lineage_scope(db):
        db._conn.execute('BEGIN')
        assert db.list_sessions_rich()
        db._conn.rollback()
    statements=[]
    assert listed(store,statements)['first']==['owner','first']
    assert len(scans(statements))==1


def test_commit_during_receipt_scan_is_not_published(store):
    changed=False
    statements=[]
    with SessionDB(db_path=store,read_only=True) as db, listing_lineage_scope(db):
        def trace(sql):
            nonlocal changed
            statements.append(sql)
            if not changed and sql.startswith('SELECT value FROM state_meta'):
                changed=True
                with sqlite3.connect(store) as writer:
                    writer.execute('UPDATE state_meta SET value=? WHERE key=?',(json.dumps({'session_id':'owner','lineage':['owner','other']}),POLICY_PREFIX+'owner'))
        db._conn.set_trace_callback(trace)
        db.list_sessions_rich()
    assert changed
    assert listed(store,statements)['other']==['owner','other']
    assert len(scans(statements))==2


def test_cached_receipt_lists_are_not_shared_with_callers(store):
    from hermes_state_local import _lineage_receipts
    with SessionDB(db_path=store,read_only=True) as db, listing_lineage_scope(db):
        first=_lineage_receipts(db._conn)
        first[0][1].append('foreign')
        assert _lineage_receipts(db._conn)==[('owner',['owner','first'])]


def test_receipt_cache_retains_at_most_sixteen_file_versions(store):
    import hermes_state_local as local
    for i in range(20):
        with sqlite3.connect(store) as writer:
            writer.execute("UPDATE sessions SET title=? WHERE id='other'",(str(i),))
        listed(store)
    assert len(local._LINEAGE_RECEIPTS)<=16
