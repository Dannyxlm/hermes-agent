"""Authoritative unread mutations cover compression aliases, not explicit forks."""
import json
import sqlite3

import pytest

from gateway.session_contract import Principal
from hermes_state import SessionDB
from hermes_state_mutation_binding import BINDING_PREFIX, authorize_history
import hermes_state_runtime as rt


FAMILY = {'root', 'tip', 'stale'}
ALL_IDS = FAMILY | {'branch'}


@pytest.fixture
def family_store(tmp_path):
    home = tmp_path / 'profiles' / 'ops'
    home.mkdir(parents=True)
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session('root', 'cli', profile_name='ops', user_id='human')
        db._write_sql("UPDATE sessions SET end_reason='compression', ended_at=101 WHERE id='root'")
        for sid in ('tip', 'stale'):
            db.create_session(sid, 'cli', parent_session_id='root', profile_name='ops', user_id='human')
        db._write_sql("UPDATE sessions SET end_reason='ws_orphan_reap', ended_at=102 WHERE id='stale'")
        db.create_session('branch', 'cli', parent_session_id='root', profile_name='ops', user_id='human',
                          model_config={'_branched_from': 'root'})
        yield db, rt.begin_runtime_epoch(db, instance_id='owner')


def mutation(epoch, session_id, request_id, *, revision=0, unread=True):
    return dict(epoch=epoch, principal_id='human', session_id=session_id, request_id=request_id,
                expected_revision=revision, expected_generation=0, operation='sidebar',
                payload={'unread': unread})


def snapshot(db):
    return {sid: db.get_session(sid) for sid in ALL_IDS}


@pytest.mark.parametrize('target', ['root', 'tip', 'stale'])
def test_alias_roundtrip_advances_exact_family_revisions_once(family_store, monkeypatch, target):
    db, epoch = family_store
    # An explicitly foreign branch is outside the selector, so it must not veto a family read.
    db._write_sql("UPDATE sessions SET profile_name='other', user_id='other' WHERE id='branch'")
    branch = db.get_session('branch')
    assert set(db.get_compression_family(target)) == FAMILY
    assert db.get_compression_family(target)[-1] == 'tip'
    for revision, unread in enumerate((True, False)):
        monkeypatch.setattr('hermes_state_mutation_read.time.time', lambda: 200)
        args = mutation(epoch, target, f'read-{revision}', revision=revision, unread=unread)
        receipt = rt.mutate_runtime_session(db, **args)
        assert receipt == {'session_id': target, 'revision': revision + 1, 'execution_generation': 0,
                           'operation': 'sidebar', 'unread': unread}
        for sid in FAMILY:
            row = db.get_session(sid)
            assert row['last_read_at'] == (0.0 if unread else 200)
            assert row['runtime_revision'] == revision + 1
            assert row['runtime_generation'] == 0
        assert db.get_session('branch') == branch
        before_retry = snapshot(db)
        monkeypatch.setattr('hermes_state_mutation_read.time.time', lambda: 300)
        assert rt.mutate_runtime_session(db, **args) == receipt
        assert snapshot(db) == before_retry
        # A different request through any alias competes with the same family revision.
        for alias in FAMILY:
            with pytest.raises(rt.RuntimeStoreError, match='revision_conflict'):
                rt.mutate_runtime_session(db, **mutation(epoch, alias, f'stale-{revision}-{alias}',
                                                         revision=revision))
    with pytest.raises(rt.RuntimeStoreError, match='stale_generation'):
        rt.mutate_runtime_session(db, **(mutation(epoch, target, 'wrong-generation', revision=2)
                                         | {'expected_generation': 1}))
    assert snapshot(db) == before_retry


def test_explicit_branch_read_never_updates_parent_family(family_store):
    db, epoch = family_store
    before = snapshot(db)
    receipt = rt.mutate_runtime_session(db, **mutation(epoch, 'branch', 'branch-read'))
    assert receipt['revision'] == 1
    assert db.get_session('branch')['last_read_at'] == 0.0
    assert db.get_session('branch')['runtime_revision'] == 1
    assert {sid: db.get_session(sid) for sid in FAMILY} == {sid: before[sid] for sid in FAMILY}


@pytest.mark.parametrize('claimed_anchor', [False, True])
@pytest.mark.parametrize('foreign_segment', ['root', 'tip', 'stale'])
@pytest.mark.parametrize('field,reason', [
    ('profile_name', 'profile_mismatch'), ('user_id', 'permission_denied'),
    ('session_key', 'permission_denied'), ('chat_id', 'permission_denied'),
    ('binding', 'permission_denied'),
])
def test_foreign_family_member_rolls_back_entire_sidebar_edit(
        family_store, foreign_segment, field, reason, claimed_anchor):
    db, epoch = family_store
    if field == 'binding':
        db._write_sql('INSERT INTO state_meta(key,value) VALUES(?,?)',
                      (BINDING_PREFIX + foreign_segment, json.dumps(['foreign-profile', 'other'])))
    else:
        db._write_sql(f'UPDATE sessions SET {field}=? WHERE id=?', ('other', foreign_segment))
    # Keep the requested anchor authorized; the foreign row is an ancestor, tip, or stale sibling.
    target = 'tip' if foreign_segment != 'tip' else 'root'
    before = snapshot(db)
    args = mutation(epoch, target, 'foreign-read')
    args['payload'] = {'title': 'must roll back', 'unread': False}
    if claimed_anchor:
        actor = Principal('human', str(db.db_path.parent), frozenset({'session:create'}), 'http')

        def authorize_write(conn):
            assert authorize_history(conn, actor, target, claim=True)

        args['_authorize_write'] = authorize_write
    with pytest.raises(rt.RuntimeStoreError, match=reason):
        rt.mutate_runtime_session(db, **args)
    assert snapshot(db) == before
    assert db._read_one("SELECT COUNT(*) FROM state_meta WHERE key LIKE 'gateway.mutation.%'")[0] == 0


def test_receipt_failure_rolls_back_watermarks_and_family_revisions(family_store):
    db, epoch = family_store
    before = snapshot(db)
    db._write_sql("CREATE TRIGGER fail_read_receipt BEFORE INSERT ON state_meta "
                  "WHEN NEW.key LIKE 'gateway.mutation.%' "
                  "BEGIN SELECT RAISE(ABORT, 'read receipt failed'); END")
    args = mutation(epoch, 'stale', 'atomic-read')
    args['payload'] = {'title': 'must roll back', 'unread': True}
    with pytest.raises(sqlite3.IntegrityError, match='read receipt failed'):
        rt.mutate_runtime_session(db, **args)
    assert snapshot(db) == before
    assert db._read_one("SELECT COUNT(*) FROM state_meta WHERE key LIKE 'gateway.mutation.%'")[0] == 0
    db._write_sql('DROP TRIGGER fail_read_receipt')
    # The same request is still admissible after rollback; read-only scope stays branch-excluding.
    args['payload'] = {'unread': True}
    assert rt.mutate_runtime_session(db, **args)['revision'] == 1
    assert db.get_session('branch') == before['branch']


@pytest.mark.parametrize('target', ['root', 'tip', 'stale'])
def test_cold_history_claim_keeps_same_owner_aliases_readable(family_store, target):
    db, epoch = family_store
    actor = Principal('human', str(db.db_path.parent), frozenset({'session:create'}), 'http')

    def authorize_write(conn):
        assert authorize_history(conn, actor, target, claim=True)

    args = mutation(epoch, target, 'cold-read') | {'_authorize_write': authorize_write}
    assert rt.mutate_runtime_session(db, **args)['revision'] == 1
    assert rt.mutate_runtime_session(db, **args)['revision'] == 1
    for sid in FAMILY:
        assert db.get_session(sid)['last_read_at'] == 0.0
        assert db.get_session(sid)['runtime_revision'] == 1
    assert db.get_session('branch')['last_read_at'] is None
    assert db.get_session('branch')['runtime_revision'] == 0


def test_import_binding_does_not_claim_unbound_family_history(family_store):
    db, epoch = family_store
    db._write_sql("UPDATE sessions SET source='import' WHERE id='tip'")
    db._write_sql('INSERT INTO state_meta(key,value) VALUES(?,?)',
                  (BINDING_PREFIX + 'tip', json.dumps([str(db.db_path.parent), 'human'])))
    before = snapshot(db)
    with pytest.raises(rt.RuntimeStoreError, match='permission_denied'):
        rt.mutate_runtime_session(db, **mutation(epoch, 'tip', 'import-read'))
    assert snapshot(db) == before
