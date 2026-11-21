"""Meeting markers are durable, additive room identity metadata, not a scheduler."""
import sqlite3

import pytest

from gateway import hosted_rooms as rooms
from persistence.gateway.hosted_rooms import RoomConflictError
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle


MEETING = {'kind': 'standup', 'schedule_local': '08:15', 'timezone': 'America/Edmonton'}


def _create(store, **changes):
    return rooms.create_room(store, placement="shared", **{
        'room_id': 'meeting-fixture', 'name': 'Meeting fixture', 'members': [],
        'authority_gateway_id': 'gateway-fixture', 'meeting': MEETING, **changes})


@pytest.mark.parametrize('meeting', [MEETING, {
    'kind': 'campfire', 'schedule_local': '03:42', 'timezone': 'Europe/Paris'}])
def test_meeting_marker_survives_reopen_rename_and_idempotent_create(tmp_path, meeting):
    store = storage_handle(tmp_path)
    created = _create(store, meeting=meeting)
    assert created['meeting'] == meeting
    retry = _create(store, meeting=dict(reversed(list(meeting.items()))))
    assert retry['idempotent'] is True
    assert retry['meeting'] == meeting
    assert retry['revision'] == created['revision']
    store.close()
    store = storage_handle(tmp_path)
    assert rooms.room_state(store, placement="shared", room_id='meeting-fixture')['meeting'] == meeting
    assert rooms.list_rooms(store, placement="shared")[0]['meeting'] == meeting
    renamed = rooms.rename_room(store, placement="shared", room_id='meeting-fixture', event_id='rename', name='Renamed')
    assert renamed['meeting'] == meeting
    assert rooms.rename_room(store, placement="shared", room_id='meeting-fixture', event_id='rename', name='Renamed')['meeting'] == meeting


@pytest.mark.parametrize('meeting', [None, {**MEETING, 'kind': 'campfire'},
    {**MEETING, 'schedule_local': '21:00'}, {**MEETING, 'timezone': 'UTC'}])
def test_conflicting_meeting_marker_does_not_mutate_existing_room(tmp_path, meeting):
    store = storage_handle(tmp_path)
    _create(store)
    with pytest.raises(RoomConflictError):
        _create(store, meeting=meeting)
    assert rooms.room_state(store, placement="shared", room_id='meeting-fixture')['meeting'] == MEETING


@pytest.mark.parametrize('meeting', [
    {}, [], 'standup', {**MEETING, 'kind': 'other'}, {**MEETING, 'kind': []},
    {**MEETING, 'schedule_local': '24:00'}, {**MEETING, 'schedule_local': '8:15'},
    {**MEETING, 'schedule_local': '08:60'}, {**MEETING, 'schedule_local': 815},
    {**MEETING, 'timezone': 'Not/A_Timezone'}, {**MEETING, 'timezone': '../UTC'},
    {**MEETING, 'timezone': None}, {**MEETING, 'extra': 'unrecognized'},
])
def test_invalid_meeting_marker_is_rejected_before_room_creation(tmp_path, meeting):
    store = storage_handle(tmp_path)
    with pytest.raises(rooms.HostedRoomError, match='meeting'):
        _create(store, meeting=meeting)
    assert rooms.list_rooms(store, placement="shared") == []


def test_old_room_migration_is_nullable_and_preserves_history(tmp_path):
    path = tmp_path / 'shared-state.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE hosted_rooms (
                room_id TEXT PRIMARY KEY, name TEXT NOT NULL, members_json TEXT NOT NULL,
                authority_gateway_id TEXT NOT NULL, authority_epoch INTEGER NOT NULL DEFAULT 1,
                next_seq INTEGER NOT NULL DEFAULT 1, event_bytes INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, disbanded_at REAL);
            INSERT INTO hosted_rooms VALUES ('ordinary', 'Old room', '[]', 'gw', 1, 1, 0, 1, 10, 10, NULL);
            PRAGMA user_version=77;
        ''')
    store = storage_handle(tmp_path)
    first = rooms.room_state(store, placement="shared", room_id='ordinary')
    assert first['meeting'] is None
    assert first['name'] == 'Old room' and first['created_at'] == 10
    assert rooms.list_rooms(store, placement="shared")[0]['meeting'] is None
    ordinary = _create(store, room_id='new-ordinary', meeting=None)
    assert ordinary['meeting'] is None
    with pytest.raises(RoomConflictError):
        _create(store, room_id='new-ordinary')
    event = rooms.rename_room(store, placement="shared", room_id='ordinary', event_id='rename-old', name='Renamed')
    assert event['meeting'] is None
    assert rooms.room_state(store, placement="shared", room_id='ordinary')['meeting'] is None
    assert rooms.read_events(store, placement="shared", room_id='ordinary')['events'][0]['payload'] == {'name': 'Renamed'}
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 77
        assert [row[1] for row in conn.execute('PRAGMA table_info(hosted_rooms)')].count('meeting_json') == 1
