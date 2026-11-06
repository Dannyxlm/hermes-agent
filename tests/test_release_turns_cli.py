"""The release-cut wire is the real read-only producer, not a protocol double.

This file is intentionally root-level per the allocated integration test scope.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt
import hermes_state_release as release


CLI = Path(__file__).resolve().parents[1] / 'hermes_cli' / 'release_turns.py'
SCHEMA = 'hermes-release-evidence.v1'
BOUND = 'sqlite_ledger_and_turn_leases_at_snapshot'


def invoke(tmp_path, request, *, raw=False):
    isolated = tmp_path / 'command-home'
    isolated.mkdir(exist_ok=True)
    # Neither cwd nor PYTHONPATH may select a different producer under -I.
    (isolated / 'hermes_state_release.py').write_text("raise RuntimeError('WRONG TREE')\n")
    env = {'PATH': os.defpath, 'HOME': str(isolated), 'TMPDIR': str(isolated),
           'HERMES_HOME': str(isolated), 'PYTHONPATH': str(isolated)}
    proc = subprocess.run([sys.executable, '-I', '-B', str(CLI)],
        input=request if raw else json.dumps(request).encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=isolated, env=env, timeout=20)
    assert proc.stdout, proc.stderr.decode('utf-8', errors='replace')
    return proc, json.loads(proc.stdout)


def snapshot_request(home):
    return {'schema_version': SCHEMA, 'operation': 'snapshot',
            'profiles': [{'profile': 'default', 'home': str(home)}]}


def test_isolated_command_raw_abi_nullable_queue_claim_steering_worker_tombstones(tmp_path):
    home = tmp_path / 'profile?with#uri-characters'
    home.mkdir()
    path = home / 'state.db'
    with SessionDB(path) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='test-command-owner')
        for sid in ('work', 'lead', 'historical-idle'):
            db.create_session(sid, source='api_server')
        for sid in ('work', 'lead'):
            rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id=sid,
                request_id=sid, payload={'text': 'SECRET CONTENT'})
        claim = rt.claim_session_input(db, epoch=epoch, session_id='work')
        rt.register_worker_execution(db, epoch=epoch, execution_id='worker',
            session_id='work', generation=claim['generation'], kind='compute',
            adoption_secret='SECRET ADOPTION')
        steering = rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id='work',
            request_id='steer', payload={'text': 'SECRET STEER'}, intent='steer')
        rt.steer_session_input(db, epoch=epoch, admission_id=steering['admission_id'],
            generation=claim['generation'])
        queued = rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id='work',
            request_id='queue', payload={'text': 'SECRET QUEUE'})
        proc, wire = invoke(tmp_path, snapshot_request(home))
        assert proc.returncode == 0 and proc.stderr == b''
        actual = json.loads(json.dumps(asdict(release.snapshot_release_evidence(path))))
        assert wire == {'schema_version': SCHEMA, 'profiles': [{'profile': 'default', 'batch': actual}],
                        'coverage_bound': BOUND}
        assert actual['scope'] is None and not actual['issues']
        assert 'lead' in {row['key']['target_session_id'] for row in actual['rows']}
        saved = [row['key'] for row in actual['rows'] if row['key']['target_session_id'] == 'work']
        queue_key = next(key for key in saved if key['id'] == queued['admission_id'])
        assert queue_key['generation'] is None and queue_key['owner_epoch'] == epoch
        assert next(row for row in actual['rows'] if row['key']['id'] == steering['admission_id'])['outcome'] == 'steering'
        rt.settle_session_input(db, epoch=epoch, admission_id=claim['admission_id'],
            generation=claim['generation'], outcome='completed', _transfers={steering['admission_id']: True})
        next_epoch = rt.begin_runtime_epoch(db, instance_id='test-command-next-owner')
        next_claim = rt.claim_session_input(db, epoch=next_epoch, session_id='work')
        request = snapshot_request(home) | {'operation': 'lookup',
            'keys': [{'profile': 'default', 'key': key} for key in saved]}
        proc, during = invoke(tmp_path, request)
        assert proc.returncode == 0 and proc.stderr == b''
        rows = during['profiles'][0]['batch']['rows']
        queue_row = next(row for row in rows if row['key']['id'] == queued['admission_id'])
        assert queue_row['status'] == 'started' and queue_row['key']['owner_epoch'] == next_epoch
        assert queue_key['generation'] is None  # captured raw key is not rewritten
        rt.settle_session_input(db, epoch=next_epoch, admission_id=next_claim['admission_id'],
            generation=next_claim['generation'], outcome='completed')
        from hermes_state_mutation_retirement import retire_terminal_receipts
        db._execute_write(lambda c: retire_terminal_receipts(c, ['work']))
        proc, retired = invoke(tmp_path, request)
        batch = retired['profiles'][0]['batch']
        assert proc.returncode == 0 and proc.stderr == b''
        assert not batch['missing_keys'] and 'lineage_unavailable' in batch['issues']
        assert all(row['storage'] == 'tombstone' and row['status'] == 'terminal' for row in batch['rows'])
        assert 'SECRET' not in json.dumps([wire, during, retired])


def test_explicit_missing_profiles_and_old_schema_are_incomplete_not_created(tmp_path):
    missing = tmp_path / 'missing-profile'
    old = tmp_path / 'old-profile'
    old.mkdir()
    import sqlite3
    with sqlite3.connect(old / 'state.db') as conn:
        conn.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY)')
    request = snapshot_request(missing)
    request['profiles'].append({'profile': 'old', 'home': str(old)})
    proc, wire = invoke(tmp_path, request)
    assert proc.returncode == 0 and proc.stderr == b''
    assert [row['profile'] for row in wire['profiles']] == ['default', 'old']
    assert 'database_unavailable' in wire['profiles'][0]['batch']['issues']
    assert 'schema_incomplete' in wire['profiles'][1]['batch']['issues']
    assert 'authority_unavailable' in wire['profiles'][1]['batch']['issues']
    assert not missing.exists()


@pytest.mark.parametrize('key_change', [
    {'owner_epoch': True}, {'generation': '1'}, {'generation': -1},
    {'kind': 'execution', 'owner_epoch': None, 'generation': 1},
    {'kind': 'execution', 'generation': None}, {'id': ''}, {'id': 'x' * 4097},
    {'target_session_id': ''}, {'kind': 'replay'}, {'payload': 'SECRET'},
])
def test_lookup_key_validation_preserves_raw_fences_and_rejects_bad_values(tmp_path, key_change):
    request = snapshot_request(tmp_path / 'missing')
    key = {'kind': 'admission', 'id': 'saved', 'target_session_id': 'work',
           'owner_epoch': 1, 'generation': None} | key_change
    request |= {'operation': 'lookup', 'keys': [{'profile': 'default', 'key': key}]}
    proc, wire = invoke(tmp_path, request)
    assert proc.returncode != 0 and proc.stderr == b''
    assert wire == {'schema_version': SCHEMA, 'error': {'code': 'invalid_request'}}


def test_lookup_explicit_missing_profile_preserves_nullable_missing_key(tmp_path):
    missing = tmp_path / 'missing'
    key = {'kind': 'admission', 'id': 'saved', 'target_session_id': 'work',
           'owner_epoch': None, 'generation': None}
    request = snapshot_request(missing) | {'operation': 'lookup',
        'keys': [{'profile': 'default', 'key': key}]}
    proc, wire = invoke(tmp_path, request)
    batch = wire['profiles'][0]['batch']
    assert proc.returncode == 0 and proc.stderr == b''
    assert batch['scope'] is None and batch['missing_keys'] == [key]
    assert batch['issues'] == ['database_unavailable'] and not missing.exists()
    request['keys'][0]['profile'] = 'undeclared'
    proc, wire = invoke(tmp_path, request)
    assert proc.returncode != 0 and wire['error']['code'] == 'invalid_request'


def test_missing_authority_and_oversized_response_are_not_success_proofs(tmp_path):
    home = tmp_path / 'authority-profile'
    home.mkdir()
    with SessionDB(home / 'state.db') as db:
        proc, wire = invoke(tmp_path, snapshot_request(home))
        assert proc.returncode == 0 and proc.stderr == b''
        batch = wire['profiles'][0]['batch']
        assert batch['authority_epoch'] is None and batch['issues'] == ['authority_unavailable']
        rt.begin_runtime_epoch(db, instance_id='synthetic')
        # Exercise the output cap against actual SQLite metadata, not a fake
        # producer result. Malformed/oversized stores cannot flood the wire.
        db._execute_write(lambda c: c.execute('UPDATE runtime_epoch SET instance_id=?',
                                             ('M' * (8 * 1024 * 1024),)))
        proc, wire = invoke(tmp_path, snapshot_request(home))
        assert proc.returncode != 0 and proc.stderr == b'' and len(proc.stdout) < 256
        assert wire == {'schema_version': SCHEMA, 'error': {'code': 'response_too_large'}}


@pytest.mark.parametrize('raw', [
    b'', b'{SECRET malformed', b'[]', b'{}', b'{"operation":NaN}',
    b'{"operation":"snapshot","operation":"lookup"}',
    b'"' + b'x' * (1024 * 1024) + b'"', b'\xff', b'[' * 2000,
])
def test_malformed_transport_is_bounded_coarse_structured_nonzero(tmp_path, raw):
    proc, wire = invoke(tmp_path, raw, raw=True)
    assert proc.returncode != 0 and proc.stderr == b''
    assert wire == {'schema_version': SCHEMA, 'error': {'code': 'invalid_request'}}
    assert len(proc.stdout) < 256


@pytest.mark.parametrize('change', [
    {'extra_private_field': 'SECRET'}, {'profiles': []}, {'profiles': [{'profile': 'default'}]},
    {'operation': 'replay'}, {'schema_version': 'wrong'}, {'keys': []},
])
def test_request_allowlist_rejects_unknown_fields_and_unsafe_shapes(tmp_path, change):
    request = snapshot_request(tmp_path / 'missing') | change
    proc, wire = invoke(tmp_path, request)
    assert proc.returncode != 0 and proc.stderr == b''
    assert wire == {'schema_version': SCHEMA, 'error': {'code': 'invalid_request'}}
