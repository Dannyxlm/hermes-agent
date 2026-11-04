"""A bounded thread replay must retain the parent of every retained turn receipt."""
from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint, MAX_TRANSCRIPT_POLICY_EVENTS
from tests.gateway.test_hosted_room_discussion import (
    GATEWAY_ID, LOCAL_PROFILES, MEMBERS, ROOM_ID, _append_activity, _append_publication, _append_user, _events,
)


def test_repeated_discussions_survive_transcript_window_eviction(tmp_path):
    db = tmp_path / 'state.db'
    room = hosted_rooms.create_room(db, room_id=ROOM_ID, name='Daily room', members=MEMBERS,
                                    authority_gateway_id=GATEWAY_ID)
    checkpoint = HostedRoomPolicyCheckpoint(db)
    for index in range(10):
        source = _append_user(db, event_id=f'user-{index}', text='Everyone share.', thread_id='main')
        while True:
            events = _events(db)
            snapshot = checkpoint.snapshot(room_id=ROOM_ID, latest_seq=events[-1]['seq'])
            decision = discussion.plan_next_task(room, snapshot.events, local_profiles=LOCAL_PROFILES,
                                                initial_watermarks=snapshot.watermarks)
            assert len(snapshot.events) <= MAX_TRANSCRIPT_POLICY_EVENTS
            if decision.status != 'task':
                assert decision.status == 'settled'
                _append_activity(db, event_id=f'settled-{index}', discussion_event_id=source['event_id'], thread_id='main')
                break
            assert decision.task is not None
            publication = discussion.plan_publication(room, events, decision.task, status='settled',
                                                      result={'text': 'Fixture reply.'}, local_profiles=LOCAL_PROFILES)
            _append_publication(db, publication)
    assert len([e for e in _events(db) if e['kind'] == 'message.member']) == 30
