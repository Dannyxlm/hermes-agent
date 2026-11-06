"""Release evidence is metadata-only, scope-bound and pinned to durable identities."""
from dataclasses import asdict
import json
import sqlite3

import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt
import hermes_state_release as release


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(path) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='synthetic-owner')
        for sid in ('work', 'lead', 'foreign', 'legacy'):
            db.create_session(sid, source='api_server')
        # Age cannot remove a long-running obligation from a restart snapshot.
        db._execute_write(lambda c: c.execute('UPDATE sessions SET started_at=1'))
        admitted = {}
        for sid in ('work', 'lead', 'foreign'):
            admitted[sid] = rt.admit_session_input(db, epoch=epoch, principal_id='api',
                session_id=sid, request_id='request-' + sid,
                payload={'text': 'SECRET INPUT', 'messages': ['SECRET HISTORY']})
        claim = rt.claim_session_input(db, epoch=epoch, session_id='work')
        execution_id = 'admission-worker:' + claim['admission_id']
        rt.register_worker_execution(db, epoch=epoch, execution_id=execution_id,
            session_id='work', generation=claim['generation'], kind='compute',
            adoption_secret='SECRET ADOPTION')
        steer = rt.admit_session_input(db, epoch=epoch, principal_id='api',
            session_id='work', request_id='steer', payload={'text': 'SECRET STEER'}, intent='steer')
        rt.steer_session_input(db, epoch=epoch, admission_id=steer['admission_id'],
            generation=claim['generation'])
        queued = rt.admit_session_input(db, epoch=epoch, principal_id='api',
            session_id='work', request_id='queue', payload={'text': 'SECRET QUEUED'})
        yield path, db, epoch, claim, steer, queued, execution_id, admitted


def _scope():
    return release.ReleaseScope(session_ids=('work', 'lead'), excluded_session_ids=('lead',))


def _settle(db, epoch, claim, steer):
    rt.settle_session_input(db, epoch=epoch, admission_id=claim['admission_id'],
        generation=claim['generation'], outcome='completed',
        _transfers={steer['admission_id']: True})


def test_whole_profile_snapshot_includes_lead_and_new_admission_not_historical_idle(ledger):
    path, db, epoch, _, _, _, _, admitted = ledger
    db.create_session('new-at-start', source='api_server')
    new = rt.admit_session_input(db, epoch=epoch, principal_id='api',
        session_id='new-at-start', request_id='new', payload={'text': 'SECRET NEW'})
    batch = release.snapshot_release_evidence(path, scope=None)
    assert batch.scope is None and batch.complete
    assert {admitted['lead']['admission_id'], admitted['foreign']['admission_id'],
            new['admission_id']} <= {row.key.id for row in batch.rows}
    assert 'SECRET' not in json.dumps(asdict(batch))


def test_stale_pre_enumerated_scope_cannot_certify_whole_profile_coverage(ledger):
    path, db, epoch, _, _, _, _, _ = ledger
    earlier_scope = release.ReleaseScope(('work', 'lead', 'foreign'))
    db.create_session('racing-new-target', source='api_server')
    arrival = rt.admit_session_input(db, epoch=epoch, principal_id='api',
        session_id='racing-new-target', request_id='racing-new-target', payload={})
    # Planted old integration: its narrower explicit scope can truthfully pass,
    # but mislabelling that as whole-profile coverage loses this admitted ID.
    scoped = release.snapshot_release_evidence(path, scope=earlier_scope)
    assert scoped.complete and arrival['admission_id'] not in {r.key.id for r in scoped.rows}
    whole = release.snapshot_release_evidence(path, scope=None)
    assert whole.complete and arrival['admission_id'] in {r.key.id for r in whole.rows}


def test_unrelated_legacy_lease_is_not_certified_by_same_target_ledger(ledger):
    path, db, _, _, _, _, _, _ = ledger
    # A running/started ledger row has no persisted link to an arbitrary lease
    # holder. The reader must not invent that link merely from a shared root.
    assert db.try_acquire_session_turn_lease('work', 'unrelated-legacy-holder')
    batch = release.snapshot_release_evidence(path, scope=None)
    assert not batch.complete
    assert 'lease_identity_unavailable' in batch.issues


def test_whole_profile_discovery_and_settlement_share_read_linearization(ledger, monkeypatch):
    path, db, epoch, claim, steer, _, worker, _ = ledger
    original = release._read_admissions

    def interleave(*args, **kwargs):
        rows = original(*args, **kwargs)
        _settle(db, epoch, claim, steer)
        db.create_session('after-linearization', source='api_server')
        rt.admit_session_input(db, epoch=epoch, principal_id='api',
            session_id='after-linearization', request_id='late', payload={})
        return rows

    monkeypatch.setattr(release, '_read_admissions', interleave)
    batch = release.snapshot_release_evidence(path, scope=None)
    assert batch.complete and batch.scope is None
    rows = {row.key.id: row for row in batch.rows}
    assert rows[claim['admission_id']].status == 'started'
    assert rows[worker].status == 'registered'
    assert 'after-linearization' not in {row.key.target_session_id for row in batch.rows}


def test_whole_profile_untracked_active_compression_root_lease_not_idle_history(ledger):
    path, db, epoch, _, _, _, _, _ = ledger
    db.create_session('compression-child', source='subagent', parent_session_id='legacy')
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET end_reason='compression' WHERE id='legacy'"))
    assert db.try_acquire_session_turn_lease('compression-child', 'synthetic-legacy-holder')
    active = release.snapshot_release_evidence(path, scope=None)
    assert not active.complete and 'untracked_active_lease' in active.issues
    # The actual writer keys this continuation's lease to the compressed root.
    assert db._session_turn_lease_key('compression-child') == 'legacy'
    admission = rt.admit_session_input(db, epoch=epoch, principal_id='api',
        session_id='compression-child', request_id='compressed-turn', payload={})
    # A queued arrival is not evidence for the legacy turn holding this lease.
    queued_only = release.snapshot_release_evidence(path, scope=None)
    assert not queued_only.complete and 'untracked_active_lease' in queued_only.issues
    claimed = rt.claim_session_input(db, epoch=epoch, session_id='compression-child')
    assert isinstance(claimed, dict)
    tracked = release.snapshot_release_evidence(path, scope=None)
    assert not tracked.complete and tracked.issues == ('lease_identity_unavailable',)
    assert admission['admission_id'] in {row.key.id for row in tracked.rows}
    rt.settle_session_input(db, epoch=epoch, admission_id=claimed['admission_id'],
        generation=claimed['generation'], outcome='completed')
    assert not release.snapshot_release_evidence(path, scope=None).complete
    db._execute_write(lambda c: c.execute('UPDATE session_turn_leases SET expires_at=0'))
    assert release.snapshot_release_evidence(path, scope=None).complete
    db._execute_write(lambda c: c.execute('DROP TABLE session_turn_leases'))
    missing = release.snapshot_release_evidence(path, scope=None)
    assert not missing.complete and 'schema_incomplete' in missing.issues


def test_whole_profile_lookup_uses_only_saved_keys_and_exact_tombstones(ledger):
    path, db, epoch, claim, steer, queued, _, _ = ledger
    before = release.snapshot_release_evidence(path, scope=None)
    keys = tuple(row.key for row in before.rows if row.key.target_session_id == 'work')
    _settle(db, epoch, claim, steer)
    rt.cancel_session_input(db, epoch=epoch, admission_id=queued['admission_id'])
    later = rt.admit_session_input(db, epoch=epoch, principal_id='api',
        session_id='foreign', request_id='not-captured', payload={})
    after = release.lookup_release_evidence(path, scope=None, keys=keys)
    assert after.scope is None and after.complete
    assert {row.key.id for row in after.rows} == {key.id for key in keys}
    assert later['admission_id'] not in {row.key.id for row in after.rows}
    assert all(row.terminal for row in after.rows)
    from hermes_state_mutation_retirement import retire_terminal_receipts
    db._execute_write(lambda c: retire_terminal_receipts(c, ['work']))
    retired = release.lookup_release_evidence(path, scope=None, keys=keys)
    assert not retired.missing_keys and all(row.storage == 'tombstone' for row in retired.rows)
    assert 'lineage_unavailable' in retired.issues
    assert not release.lookup_release_evidence(path, scope=None, keys=()).rows


def test_whole_profile_read_is_pinned_before_discovery_or_authority_interleaving(ledger, monkeypatch):
    path, db, epoch, _, _, _, _, _ = ledger
    original = release._authority
    admitted = []

    def interleave(conn, issues):
        # At this seam no authority, session IDs, or ledger rows have been
        # returned yet. The reader must already have pinned its WAL read.
        next_epoch = rt.begin_runtime_epoch(db, instance_id='racing-owner')
        db.create_session('racing-session', source='api_server')
        admitted.append(rt.admit_session_input(db, epoch=next_epoch, principal_id='api',
            session_id='racing-session', request_id='racing', payload={}))
        return original(conn, issues)

    monkeypatch.setattr(release, '_authority', interleave)
    before = release.snapshot_release_evidence(path, scope=None)
    assert before.complete and before.authority_epoch == epoch
    assert admitted[0]['admission_id'] not in {row.key.id for row in before.rows}
    monkeypatch.setattr(release, '_authority', original)
    after = release.snapshot_release_evidence(path, scope=None)
    assert after.complete and after.authority_instance_id == 'racing-owner'
    assert admitted[0]['admission_id'] in {row.key.id for row in after.rows}


@pytest.mark.parametrize('source,config', [
    ('tool', {}), ('subagent', {'_delegate_from': 'legacy'}),
    ('api_server', {'_branched_from': 'legacy'}), ('api_server', {'_reset_from': 'legacy'}),
])
def test_whole_profile_fork_ledger_cannot_cover_untracked_parent_lease(ledger, source, config):
    path, db, epoch, _, _, _, _, _ = ledger
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET end_reason='compression' WHERE id='legacy'"))
    db.create_session('fork', source=source, parent_session_id='legacy', model_config=config)
    assert db.try_acquire_session_turn_lease('legacy', 'synthetic-parent-holder')
    rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id='fork',
        request_id='fork-turn', payload={})
    assert isinstance(rt.claim_session_input(db, epoch=epoch, session_id='fork'), dict)
    assert db._session_turn_lease_key('fork') == 'fork'
    batch = release.snapshot_release_evidence(path, scope=None)
    assert not batch.complete and batch.issues == ('untracked_active_lease',)


def test_snapshot_then_terminal_exact_readback_and_retirement(ledger):
    path, db, epoch, claim, steer, queued, worker, _ = ledger
    before = release.snapshot_release_evidence(path, scope=_scope())
    assert before.complete and before.authority_epoch == epoch
    assert before.authority_instance_id == 'synthetic-owner'
    rows = {row.key.id: row for row in before.rows}
    assert set(rows) == {claim['admission_id'], steer['admission_id'], queued['admission_id'], worker}
    assert rows[worker].worker_kind == 'compute'
    assert rows[worker].key.owner_epoch == epoch
    assert rows[worker].key.generation == claim['generation']
    assert rows[claim['admission_id']].lineage == ('work',)
    assert rows[steer['admission_id']].status == 'terminal'
    assert rows[steer['admission_id']].outcome == 'steering'
    assert not rows[steer['admission_id']].terminal
    assert not any(row.completed_turn for row in before.rows)
    _settle(db, epoch, claim, steer)
    rt.cancel_session_input(db, epoch=epoch, admission_id=queued['admission_id'])
    keys = tuple(row.key for row in before.rows)
    after = release.lookup_release_evidence(path, scope=_scope(), keys=keys)
    assert after.complete and not after.missing_keys
    assert all(row.terminal for row in after.rows)
    completed = [row.key.id for row in after.rows if row.completed_turn]
    assert completed == [claim['admission_id']]
    assert not release.snapshot_release_evidence(path, scope=_scope()).rows
    # The real retirement path deletes live rows and writes metadata-bearing tombstones.
    from hermes_state_mutation_retirement import retire_terminal_receipts
    db._execute_write(lambda c: retire_terminal_receipts(c, ['work']))
    retired = release.lookup_release_evidence(path, scope=_scope(), keys=keys)
    assert {row.key.id for row in retired.rows} == set(rows)
    assert all(row.storage == 'tombstone' and row.terminal for row in retired.rows)
    assert not retired.complete  # retirement deliberately discards admission lineage
    assert 'lineage_unavailable' in retired.issues
    assert 'SECRET' not in json.dumps(asdict(retired))


def test_sql_authorizer_denies_content_before_it_can_be_redacted(ledger, monkeypatch):
    path, db, epoch, claim, steer, _, _, _ = ledger
    before = release.snapshot_release_evidence(path, scope=_scope())
    _settle(db, epoch, claim, steer)
    from hermes_state_mutation_retirement import retire_terminal_receipts
    # The still queued row must be cancelled before retirement.
    queued = rt.list_session_admissions(db, session_id='work')
    for row in queued:
        rt.cancel_session_input(db, epoch=epoch, admission_id=row['admission_id'])
    db._execute_write(lambda c: retire_terminal_receipts(c, ['work']))
    original_connect = sqlite3.connect
    reads = []
    denied = []

    def guarded_connect(*args, **kwargs):
        assert kwargs.get('uri') and '?mode=ro' in args[0]
        conn = original_connect(*args, **kwargs)

        def authorize(action, table, column, _database, _trigger):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                if table in ('messages', 'system_prompts', 'worker_receipts') or column in (
                        'payload_json', 'payload_digest', 'adoption_digest', 'system_prompt',
                        'origin_json', 'entry_json', 'result_json'):
                    denied.append((table, column))
                    return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(release.sqlite3, 'connect', guarded_connect)
    result = release.lookup_release_evidence(path, scope=_scope(), keys=tuple(row.key for row in before.rows))
    assert {row.key for row in result.rows} == {row.key for row in before.rows}
    assert [row.key.id for row in result.rows if row.completed_turn] == [claim['admission_id']]
    assert reads and not denied
    assert 'SECRET' not in json.dumps(asdict(result))
    assert not release.snapshot_release_evidence(path, scope=_scope()).rows
    whole = release.snapshot_release_evidence(path, scope=None)
    assert whole.complete and {row.key.target_session_id for row in whole.rows} == {'lead', 'foreign'}
    retired_whole = release.lookup_release_evidence(path, scope=None, keys=tuple(row.key for row in before.rows))
    assert {row.key for row in retired_whole.rows} == {row.key for row in before.rows}
    assert not denied and 'SECRET' not in json.dumps(asdict(whole))


def test_cross_scope_missing_and_wrong_execution_identity_are_incomplete(ledger):
    path, _, epoch, claim, _, _, worker, admitted = ledger
    keys = (
        release.EvidenceKey('admission', admitted['foreign']['admission_id'], 'foreign', epoch, None),
        release.EvidenceKey('admission', admitted['lead']['admission_id'], 'lead', epoch, None),
        release.EvidenceKey('execution', 'missing-worker', 'work', epoch, 1),
        release.EvidenceKey('execution', worker, 'work', epoch + 1, claim['generation']),
        release.EvidenceKey('execution', worker, 'work', epoch, claim['generation'] + 1),
    )
    result = release.lookup_release_evidence(path, scope=_scope(), keys=keys)
    assert not result.complete
    assert len(result.rows) == 1 and result.rows[0].key.owner_epoch == epoch
    assert result.missing_keys == keys[:3] + keys[4:]
    assert {'outside_scope', 'missing_identity', 'identity_mismatch', 'owner_epoch_changed'} <= set(result.issues)
    assert 'SECRET' not in json.dumps(asdict(result))


def test_restarted_authority_does_not_replace_execution_identity(ledger):
    path, db, epoch, _, _, _, _, _ = ledger
    before = release.snapshot_release_evidence(path, scope=_scope())
    restarted_epoch = rt.begin_runtime_epoch(db, instance_id='synthetic-restart')
    rt.recover_session_inputs(db, epoch=restarted_epoch)
    result = release.lookup_release_evidence(path, scope=_scope(), keys=tuple(row.key for row in before.rows))
    assert result.complete and result.authority_epoch == restarted_epoch
    assert result.authority_instance_id == 'synthetic-restart'
    assert all(row.key.owner_epoch == epoch for row in result.rows)
    assert not any(row.completed_turn for row in result.rows)
    assert 'unknown' in {row.status for row in result.rows}


def test_snapshot_is_one_sqlite_transaction_during_concurrent_settlement(ledger, monkeypatch):
    path, db, epoch, claim, steer, _, worker, _ = ledger
    original = release._read_admissions

    def interleaved(*args, **kwargs):
        rows = original(*args, **kwargs)
        _settle(db, epoch, claim, steer)
        return rows

    monkeypatch.setattr(release, '_read_admissions', interleaved)
    before = release.snapshot_release_evidence(path, scope=_scope())
    assert before.complete
    rows = {row.key.id: row for row in before.rows}
    assert rows[claim['admission_id']].status == 'started'
    assert rows[worker].status == 'registered'
    monkeypatch.setattr(release, '_read_admissions', original)
    after = release.lookup_release_evidence(path, scope=_scope(), keys=(rows[worker].key,))
    assert after.complete and after.rows[0].terminal


def test_legacy_missing_schema_and_missing_file_never_mean_completed(tmp_path, ledger):
    path, _, _, _, _, _, _, _ = ledger
    legacy = release.snapshot_release_evidence(path, scope=release.ReleaseScope(('legacy',)))
    assert not legacy.complete and not legacy.rows
    assert 'untracked_session' in legacy.issues
    old = tmp_path / 'old.db'
    with sqlite3.connect(old) as conn:
        conn.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY)')
        conn.execute("INSERT INTO sessions VALUES('legacy')")
    result = release.snapshot_release_evidence(old, scope=release.ReleaseScope(('legacy',)))
    assert not result.complete and not result.rows
    assert 'schema_incomplete' in result.issues
    missing = tmp_path / 'does-not-exist.db'
    result = release.snapshot_release_evidence(missing, scope=release.ReleaseScope(('work',)))
    assert not result.complete and 'database_unavailable' in result.issues
    assert not missing.exists()


def test_readonly_connection_cannot_write_or_create(ledger):
    path, _, _, _, _, _, _, _ = ledger
    with release._reader(path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE runtime_epoch SET epoch=999")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('CREATE TABLE forbidden_write(id TEXT)')


def test_explicit_nonempty_scope_required():
    with pytest.raises(ValueError):
        release.ReleaseScope(())
    with pytest.raises(ValueError):
        release.ReleaseScope(('',))


def test_reader_holds_the_existing_raw_file_access_guard(tmp_path):
    from hermes_cli.sqlite_safe_read import has_live_connection
    path = tmp_path / 'state.db'
    with SessionDB(path):
        pass
    assert not has_live_connection(path)
    with release._reader(path):
        assert has_live_connection(path)
    assert not has_live_connection(path)


def test_adopted_long_running_work_remains_visible_but_epoch_drift_is_incomplete(tmp_path):
    path = tmp_path / 'state.db'
    scope = release.ReleaseScope(('work',))
    with SessionDB(path) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='before-release')
        db.create_session('work', source='api_server')
        rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id='work',
            request_id='long-running', payload={'text': 'PRIVATE INPUT'})
        claimed = rt.claim_session_input(db, epoch=epoch, session_id='work')
        assert isinstance(claimed, dict)
        rt.register_worker_execution(db, epoch=epoch, execution_id='child-work',
            session_id='work', generation=claimed['generation'], kind='cron',
            adoption_secret='synthetic-adoption')
        rt.adopt_worker_execution(db, epoch=epoch, execution_id='child-work',
            session_id='work', generation=claimed['generation'], adoption_secret='synthetic-adoption')
        before = release.snapshot_release_evidence(path, scope=scope)
        assert before.complete
        keys = tuple(row.key for row in before.rows)
        next_epoch = rt.begin_runtime_epoch(db, instance_id='after-release')
        rt.recover_session_inputs(db, epoch=next_epoch)
        rt.adopt_worker_execution(db, epoch=next_epoch, execution_id='child-work',
            session_id='work', generation=claimed['generation'], adoption_secret='synthetic-adoption')
        during = release.lookup_release_evidence(path, scope=scope, keys=keys)
        assert not during.complete and not during.missing_keys
        assert during.issues == ('owner_epoch_changed',)
        assert {row.status for row in during.rows} == {'started', 'running'}
        rt.settle_session_input(db, epoch=next_epoch, admission_id=claimed['admission_id'],
            generation=claimed['generation'], outcome='completed')
        after = release.lookup_release_evidence(path, scope=scope, keys=keys)
        assert not after.complete and not after.missing_keys
        assert all(row.terminal and row.key.owner_epoch == next_epoch for row in after.rows)
        assert sum(row.completed_turn for row in after.rows) == 1


def test_queued_identity_can_be_claimed_by_next_owner_without_inventing_a_turn(ledger):
    path, db, epoch, claim, steer, queued, _, _ = ledger
    before = release.snapshot_release_evidence(path, scope=_scope())
    key = next(row.key for row in before.rows if row.key.id == queued['admission_id'])
    assert key.generation is None
    _settle(db, epoch, claim, steer)
    next_epoch = rt.begin_runtime_epoch(db, instance_id='next-queue-owner')
    started = rt.claim_session_input(db, epoch=next_epoch, session_id='work')
    assert isinstance(started, dict)
    during = release.lookup_release_evidence(path, scope=_scope(), keys=(key,))
    assert during.complete and not during.rows[0].completed_turn
    assert during.rows[0].key.owner_epoch == next_epoch
    rt.settle_session_input(db, epoch=next_epoch, admission_id=started['admission_id'],
        generation=started['generation'], outcome='completed')
    after = release.lookup_release_evidence(path, scope=_scope(), keys=(key,))
    assert after.complete and after.rows[0].completed_turn


def test_malformed_or_missing_terminal_metadata_is_not_completion(ledger):
    path, db, epoch, claim, steer, _, _, _ = ledger
    _settle(db, epoch, claim, steer)
    key = release.EvidenceKey('admission', claim['admission_id'], 'work', epoch, claim['generation'])
    db._execute_write(lambda c: c.execute('UPDATE session_admissions SET outcome=NULL WHERE admission_id=?', (key.id,)))
    result = release.lookup_release_evidence(path, scope=_scope(), keys=(key,))
    assert not result.complete and not result.rows[0].completed_turn
    assert 'outcome_unavailable' in result.issues
    db._execute_write(lambda c: c.execute('UPDATE session_admissions SET lineage_json=? WHERE admission_id=?', ('invalid', key.id)))
    result = release.lookup_release_evidence(path, scope=_scope(), keys=(key,))
    assert not result.complete and result.rows[0].lineage is None
    assert 'lineage_unavailable' in result.issues
