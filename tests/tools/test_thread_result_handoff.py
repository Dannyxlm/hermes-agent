"""Admission handoff receipts survive tail reads, reactivation and failed wakes.

Real temporary stores and production tool/wake entrypoints; owner transport is the
existing deterministic double, not evidence of a deployed delivery.
"""
import os
import time

import pytest

from tests.tools.test_thread_tool import _db, admit, call, settle, thread

pytest_plugins = ['tests.tools.test_thread_tool']


def test_same_epoch_unknown_is_uncertain_not_proof_of_restart(home):
    from types import SimpleNamespace
    from gateway.session_settlement_recovery import _mark_unknown
    from gateway.thread_wake import render
    from hermes_state_runtime import begin_runtime_epoch, get_session_admission

    with _db(home) as db:
        epoch = begin_runtime_epoch(db, instance_id='still-alive')
        thread(db, 'child')
        admit(db, 'child', 'uncertain', 'started')
        db._write_sql('UPDATE session_admissions SET owner_epoch=?', (epoch,))
        row = get_session_admission(db, admission_id='uncertain')
        assert row is not None
        assert _mark_unknown(SimpleNamespace(db=db, epoch=epoch, sessions={}), row) == 'unknown'
        current = get_session_admission(db, admission_id='uncertain')
        assert current is not None and current['owner_epoch'] == epoch
    result = call(action='wait', thread_id='child', admission_id='uncertain')['threads'][0]
    text = render([{**result, 'profile': 'default'}], [], 'default')
    for copy in (text, result['note']):
        assert 'restart' not in copy.lower()
        assert 'uncertain' in copy.lower()
        assert 'not rerun automatically' in copy.lower()


def test_tail_read_does_not_acknowledge_unseen_admission_results(home):
    from tools import thread_state as ts

    with _db(home) as db:
        thread(db, 'child')
        admit(db, 'child', 'old-result', 'terminal', 'completed', reply='older result')
        db.append_message('child', 'assistant', 'older result')
        admit(db, 'child', 'new-result', 'terminal', 'completed', reply='newest result')
        db.append_message('child', 'assistant', 'newest result')
    out = call(action='read', thread_id='child', limit=1)
    assert [message['text'] for message in out['messages']] == ['newest result']
    assert not ts.delivered(home, 'old-result', 'done')
    assert {item['admission_id'] for item in ts.wake_state(home, 'default', ['parent'])[0]} == {
        'old-result', 'new-result'}
    read = call(action='read', thread_id='child', admission_id='old-result', limit=1)
    assert read['result']['admission_id'] == 'old-result'
    assert read['result']['final_response'] == 'older result'
    assert ts.delivered(home, 'old-result', 'done')
    assert not ts.delivered(home, 'new-result', 'done')
    assert call(action='read', thread_id='child', admission_id='missing')['error'] == 'invalid_request'


def test_acknowledged_result_does_not_replay_after_old_thread_reactivation(home, owner):
    from gateway import thread_wake
    from tools import thread_state as ts

    with _db(home) as db:
        thread(db, 'child')
        admit(db, 'child', 'old-result', 'terminal', 'completed', reply='older result')
    assert call(action='wait', thread_id='child', timeout_s=1)['threads'][0]['final_response'] == 'older result'
    old = time.time() - ts.MARKER_TTL_S - 100
    os.utime(ts._marker(home, 'old-result', 'done'), (old, old))
    ts.prune_markers(home)
    with _db(home) as db:
        admit(db, 'child', 'new-result', 'terminal', 'completed', reply='fresh result')
    assert thread_wake.deliver(home, 'default', 'parent')
    update = owner.submits()[-1]
    assert 'fresh result' in update['text']
    assert 'older result' not in update['text']
    assert ts.delivered(home, 'old-result', 'done')


@pytest.mark.parametrize('outcome', ['cancelled', 'unknown'])
def test_exhausted_notice_remains_visible_and_explicit_read_recovers(home, owner, outcome):
    from gateway import thread_wake
    from tools import thread_state as ts

    with _db(home) as db:
        thread(db, 'child')
        admit(db, 'child', 'result', 'terminal', 'completed', reply='recoverable result')
    owner.update_outcome = outcome
    for _ in range(6 * (thread_wake.MAX_RESENDS + 2)):
        thread_wake.deliver(home, 'default', 'parent')
    assert len(owner.submits()) == 1 + thread_wake.MAX_RESENDS
    assert not ts.delivered(home, 'result', 'done')
    listed = call(action='list')['threads'][0]
    assert listed['needs_lead'] == ['result']
    assert call(action='read', thread_id='child', limit=1)['needs_lead'] == ['result']
    old = time.time() - ts.MARKER_TTL_S - 100
    for path in ts._marker_dir(home).iterdir():
        os.utime(path, (old, old))
    ts.prune_markers(home)
    assert call(action='list')['threads'][0]['needs_lead'] == ['result']
    # Exhaustion must not block unrelated newly completed work.
    owner.update_outcome = 'completed'
    with _db(home) as db:
        admit(db, 'child', 'next-result', 'terminal', 'completed', reply='next result')
    assert thread_wake.deliver(home, 'default', 'parent')
    assert 'recoverable result' not in owner.submits()[-1]['text']
    read = call(action='read', thread_id='child', admission_id='result')
    assert read['result']['final_response'] == 'recoverable result'
    assert ts.delivered(home, 'result', 'done')
    assert not call(action='list')['threads'][0].get('needs_lead')


def test_exhaustion_clear_receipt_loss_does_not_falsely_acknowledge(home, owner, monkeypatch):
    from gateway import thread_wake
    from tools import thread_state as ts

    monkeypatch.setattr(thread_wake, 'MAX_RESENDS', 0)
    with _db(home) as db:
        thread(db, 'child')
        admit(db, 'child', 'result', 'terminal', 'completed', reply='result')
    owner.update_outcome = 'unknown'
    assert thread_wake.deliver(home, 'default', 'parent')
    assert not thread_wake.deliver(home, 'default', 'parent')  # exhaustion saved before clear
    owner.lose_resolve_receipt = True
    with pytest.raises(ConnectionError):
        thread_wake.deliver(home, 'default', 'parent')
    assert not ts.delivered(home, 'result', 'done')
    assert not thread_wake.deliver(home, 'default', 'parent')
    assert not ts.delivered(home, 'result', 'done')
    assert call(action='list')['threads'][0]['needs_lead'] == ['result']
    assert call(action='wait', thread_id='child', admission_id='result')['threads'][0]['final_response'] == 'result'
    assert ts.delivered(home, 'result', 'done')


def test_explicit_result_read_never_borrows_a_later_turn_reply(home):
    with _db(home) as db:
        thread(db, 'child')
        admit(db, 'child', 'old', 'terminal', 'completed')  # no committed result
        admit(db, 'child', 'new', 'terminal', 'completed', reply='unrelated newer result')
        db.append_message('child', 'assistant', 'unrelated newer result')
    result = call(action='read', thread_id='child', admission_id='old')['result']
    assert result['final_response'] == ''
    assert 'unavailable' in result['error']


@pytest.mark.parametrize('legacy', [False, True])
def test_pending_notice_is_recovered_after_discovery_and_marker_windows(home, owner, legacy):
    from gateway import thread_wake
    from tools import thread_state as ts

    with _db(home) as db:
        thread(db, 'child')
        admit(db, 'child', 'result', 'terminal', 'completed', reply='result')
    owner.update_outcome = None
    assert thread_wake.deliver(home, 'default', 'parent')
    with _db(home) as db:
        db._write_sql('UPDATE work_launches SET created_at=?', (time.time() - ts.WAKE_LAUNCH_WINDOW_S - 100,))
        db._write_sql('UPDATE sessions SET last_activity_at=?, started_at=? WHERE id=?',
                      (1, 1, 'child'))
        update = db._read_one("SELECT admission_id FROM session_admissions WHERE request_id LIKE 'thread_update:%'")
        settle(db, update['admission_id'], 'completed')
    if legacy:
        import json
        path = thread_wake._record_path(home, 'parent')
        record = json.loads(path.read_text())
        record.pop('lead', None)
        path.write_text(json.dumps(record))
    old = time.time() - ts.MARKER_TTL_S - 100
    for path in ts._marker_dir(home).iterdir():
        os.utime(path, (old, old))
    sweeper = thread_wake.Sweeper(lambda: [home])
    assert sweeper.sweep() == 0
    assert ts.delivered(home, 'result', 'done')
    assert len(owner.submits()) == 1
