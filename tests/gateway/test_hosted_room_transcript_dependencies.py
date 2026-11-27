"""A bounded thread replay must retain the parent of every retained turn receipt."""
from gateway import hosted_room_discussion as discussion
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint
from tests.gateway.test_hosted_room_discussion import (
    LOCAL_PROFILES, ROOM_ID, _append_activity, _append_publication, _append_user, _events,
)

pytest_plugins = ['tests.gateway.test_hosted_room_discussion']


def test_repeated_discussions_survive_transcript_window_eviction(room_db):
    db, room = room_db
    checkpoint = HostedRoomPolicyCheckpoint(db, placement='profile')
    rounds = range(10)
    for index in rounds:
        source = _append_user(db, event_id=f'user-{index}', text='Everyone share.', thread_id='main')
        while True:
            events = _events(db)
            snapshot = checkpoint.snapshot(room_id=ROOM_ID, latest_seq=events[-1]['seq'])
            if index == rounds[-1]:
                # The durable log retains the first reply, but the policy window has evicted it.
                first_reply = next(e['event_id'] for e in events if e['kind'] == 'message.member')
                assert all(e['event_id'] != first_reply for e in snapshot.events)
            decision = discussion.plan_next_task(room, snapshot.events, local_profiles=LOCAL_PROFILES,
                                                initial_watermarks=snapshot.watermarks)
            if decision.status != 'task':
                assert decision.status == 'settled'
                _append_activity(db, event_id=f'settled-{index}', discussion_event_id=source['event_id'], thread_id='main')
                break
            assert decision.task is not None
            publication = discussion.plan_publication(room, events, decision.task, status='settled',
                                                      result={'text': 'Fixture reply.'}, local_profiles=LOCAL_PROFILES)
            _append_publication(db, publication)
    assert len([e for e in _events(db) if e['kind'] == 'message.member']) == 30
