"""Managed compression must carry the caller's turn fence into owner publication."""
import pytest

from hermes_state_errors import SessionTurnLeaseLostError


@pytest.mark.parametrize('rotate', [False, True])
@pytest.mark.parametrize('holder', [None, 'turn'])
def test_worker_compression_publishes_with_callers_turn_holder(worker, rotate, holder):
    db, store = worker
    messages = [{'role': 'user', 'content': 'retained'}]
    store.append_messages_batch('owned', messages)
    if holder:
        assert db.try_acquire_session_turn_lease('owned', holder)
    assert store.try_acquire_compression_lock('owned', 'compression')
    if rotate:
        store.publish_compression_child(parent_session_id='owned', child_session_id='child',
            source='cli', messages=messages, compression_lock_holder='compression',
            turn_lease_holder=holder)
        assert db.get_session('owned')['end_reason'] == 'compression'
        assert store.scope['session_id'] == 'child'
    else:
        assert store.archive_and_compact('owned', messages, lock_holder='compression',
            turn_lease_holder=holder) is not None
    current = store.scope['session_id']
    assert db.get_messages(current)[0]['content'] == 'retained'
    assert messages[0]['_row_id'] == db.get_messages(current)[0]['id']


@pytest.mark.parametrize('rotate', [False, True])
def test_worker_compression_rejects_reclaimed_turn_without_partial_publication(worker, rotate):
    db, store = worker
    messages = [{'role': 'user', 'content': 'retained'}]
    store.append_messages_batch('owned', messages)
    assert db.try_acquire_session_turn_lease('owned', 'old-turn')
    assert store.try_acquire_compression_lock('owned', 'compression')
    # Reclaim after the producer took its snapshot but before the owner receives publication.
    db._write_sql('UPDATE session_turn_leases SET expires_at=0')
    assert db.try_acquire_session_turn_lease('owned', 'new-turn')
    before = db.get_messages('owned', include_inactive=True)
    with pytest.raises(SessionTurnLeaseLostError, match='Session turn lease lost'):
        if rotate:
            store.publish_compression_child(parent_session_id='owned', child_session_id='child',
                source='cli', messages=messages, compression_lock_holder='compression',
                turn_lease_holder='old-turn')
        else:
            store.archive_and_compact('owned', messages, lock_holder='compression',
                turn_lease_holder='old-turn')
    assert db.get_messages('owned', include_inactive=True) == before
    assert db.get_session('owned')['ended_at'] is None
    assert db.get_session('child') is None
    assert db._read_one('SELECT session_id FROM worker_executions WHERE execution_id=?',
                        ('worker',))[0] == 'owned'
    assert db._read_one('SELECT holder FROM session_turn_leases')[0] == 'new-turn'
