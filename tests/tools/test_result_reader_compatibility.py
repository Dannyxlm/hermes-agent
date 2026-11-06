"""Older readers must preserve newer durable records without enabling newer writers."""
import json
import os
import time

import pytest

from hermes_constants import get_hermes_home
from tools import process_registry as pr
from tools import process_registry_results as results
from tools import thread_state as ts
from gateway import thread_wake as wake
from utils import atomic_json_write


def completed_record(session_id='proc_compat'):
    return {'id': session_id, 'command': 'printf synthetic', 'started_at': 1.0,
            'parent_session_id': 'owner', 'session_key': 'owner', 'exit_code': 0,
            'notify_on_complete': True, 'output': 'synthetic result',
            'notification': {'status': 'pending', 'identity': ['completion', session_id, 1.0]}}


def checkpoint(record):
    return [{'session_id': record['id'], 'completed_result': record}]


@pytest.mark.parametrize('status', ['pending', 'unknown'])
def test_unsettled_results_survive_age_and_capacity(status):
    directory = get_hermes_home() / 'logs/process-results'
    directory.mkdir(parents=True)
    record = completed_record()
    record['notification']['status'] = status
    path = directory / 'proc_compat.json'
    atomic_json_write(path, record)
    old = time.time() - results.RESULT_RETENTION_SECONDS - 1
    os.utime(path, (old, old))
    for n in range(results.MAX_RETAINED_RESULTS + 2):
        atomic_json_write(directory / f'proc_neighbor_{n}.json', {'id': f'proc_neighbor_{n}'})
    assert path in results._result_paths()
    assert json.loads(path.read_text()) == record
    assert len(list(directory.glob('proc_neighbor_*.json'))) == results.MAX_RETAINED_RESULTS


def test_foreign_checkpoint_write_preserves_completed_fallback():
    home = get_hermes_home()
    entries = checkpoint(completed_record())
    atomic_json_write(home / 'processes.json', entries)
    pr.ProcessRegistry()._write_checkpoint()
    assert json.loads((home / 'processes.json').read_text()) == entries


def test_completed_checkpoint_survives_refusal_and_recovers_without_pid_probe(monkeypatch):
    home = get_hermes_home()
    record = completed_record()
    entries = checkpoint(record)
    atomic_json_write(home / 'processes.json', entries)
    home.joinpath('logs').mkdir(exist_ok=True)
    directory = home / 'logs/process-results'
    directory.write_text('filesystem obstruction')
    registry = pr.ProcessRegistry()

    def forbidden(*args):
        pytest.fail('completed record must not probe or signal a PID')

    monkeypatch.setattr(registry, '_detached_host_fate', forbidden)
    assert registry.recover_from_checkpoint() == 0
    assert json.loads((home / 'processes.json').read_text()) == entries
    directory.unlink()
    assert registry.recover_from_checkpoint() == 0
    assert json.loads((directory / 'proc_compat.json').read_text()) == record
    assert json.loads((home / 'processes.json').read_text()) == []
    assert not registry._running


def test_recovery_does_not_overwrite_newer_disposition():
    home = get_hermes_home()
    record = completed_record()
    atomic_json_write(home / 'processes.json', checkpoint(record))
    settled = {**record, 'notification': {**record['notification'], 'status': 'terminal'}}
    directory = home / 'logs/process-results'
    directory.mkdir(parents=True)
    atomic_json_write(directory / 'proc_compat.json', settled)
    assert pr.ProcessRegistry().recover_from_checkpoint() == 0
    assert json.loads((directory / 'proc_compat.json').read_text()) == settled


def test_durable_thread_records_survive_age_but_claims_expire():
    home = get_hermes_home()
    ts.mark_delivered(home, 'seen', 'done')
    marker = ts._marker(home, 'unseen', 'done')
    needs_lead = marker.with_name(marker.name + '.needs-lead')
    needs_lead.write_text('')
    assert ts.claim(home, 'claimed', 'done')
    wake._save(home, 'owner', {'key': 'notice', 'items': [['unseen', 'done']], 'text': 'result'})
    directory = ts._marker_dir(home)
    old = time.time() - ts.MARKER_TTL_S - 1
    for path in directory.iterdir():
        os.utime(path, (old, old))
    ts.prune_markers(home)
    assert ts.delivered(home, 'seen', 'done')
    assert needs_lead.exists()
    pending = wake._load(home, 'owner')
    assert pending is not None and pending['key'] == 'notice'
    assert not list(directory.glob('*.claim'))


def test_exhausted_notice_never_acknowledges_unconsumed_result():
    home = get_hermes_home()
    record = {'key': 'notice', 'items': [['unseen', 'done']], 'text': 'result',
              'resends': wake.MAX_RESENDS, 'exhausted': True}
    wake._save(home, 'owner', record)
    admission = {'admission_id': 'cancelled-notice', 'generation': 1,
                 'status': 'terminal', 'outcome': 'cancelled'}
    assert not wake._advance(home, 'default', 'owner', 'owner', record, admission, False)
    assert not ts.delivered(home, 'unseen', 'done')
    assert (wake._load(home, 'owner') is not None
            or marker_exists(home, 'unseen'))


@pytest.mark.parametrize('record', [[], {'notification': ['invalid']}])
def test_unreadable_result_neighbor_does_not_wedge_completion(record):
    directory = get_hermes_home() / 'logs/process-results'
    directory.mkdir(parents=True)
    malformed = directory / 'proc_malformed.json'
    atomic_json_write(malformed, record)
    session = pr.ProcessSession(id='proc_valid', command='printf synthetic', started_at=1.0)
    registry = pr.ProcessRegistry()
    registry._running[session.id] = session
    registry._finish_exited(session, 0)
    assert session.exited and session._completion_event.is_set()
    assert session.id not in registry._running
    assert malformed.exists()


def test_retention_lock_failure_does_not_wedge_completion(monkeypatch):
    def refuse():
        raise RuntimeError('file lock unavailable')

    monkeypatch.setattr(results, '_result_paths', refuse)
    session = pr.ProcessSession(id='proc_valid', command='printf synthetic', started_at=1.0)
    registry = pr.ProcessRegistry()
    registry._running[session.id] = session
    registry._finish_exited(session, 0)
    assert session.exited and session._completion_event.is_set()
    assert session.id not in registry._running


@pytest.mark.parametrize('exhausted', [False, True])
def test_newer_exhausted_notice_clears_own_unknown_barrier_without_ack(monkeypatch, exhausted):
    home = get_hermes_home()
    cut = {'admission_id': 'unknown-notice', 'generation': 1}
    record = {'key': 'notice', 'items': [['unseen', 'done']], 'text': 'result',
              'resends': wake.MAX_RESENDS, 'lead': 'owner', **({'exhausted': True, 'cut': cut} if exhausted else {})}
    wake._save(home, 'owner', record)
    cleared = []
    monkeypatch.setattr(wake, '_clear', lambda h, tip, identity: cleared.append(identity))
    admission = {**cut, 'status': 'unknown', 'outcome': None}
    assert not wake._advance(home, 'default', 'owner', 'owner', record, admission, False)
    if not cleared:
        pending = wake._load(home, 'owner')
        assert pending is not None and pending.get('exhausted')
        assert not wake._advance(home, 'default', 'owner', 'owner', pending, admission, False)
    assert cleared == [cut]
    assert not ts.delivered(home, 'unseen', 'done')


def marker_exists(home, admission_id):
    marker = ts._marker(home, admission_id, 'done')
    return marker.with_name(marker.name + '.needs-lead').exists()
