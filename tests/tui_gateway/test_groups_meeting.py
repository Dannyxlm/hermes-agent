"""Legacy groups RPCs expose the same durable meeting marker as canonical RPCs."""
import pytest

import tui_gateway.server as srv
from tests.tui_gateway import test_groups_methods as group_fixtures

home = group_fixtures.home
_result = group_fixtures._result


@pytest.mark.parametrize('meeting', [None, {
    'kind': 'standup', 'schedule_local': '08:15', 'timezone': 'America/Edmonton'}])
def test_legacy_meeting_marker_on_all_room_surfaces(home, meeting):
    params = {'room_id': 'meeting-fixture', 'name': 'Meeting fixture', 'members': [
        {'member_id': 'one', 'profile': 'default', 'handle': 'one'},
        {'member_id': 'two', 'profile': 'ops', 'handle': 'two'}]}
    if meeting is not None:
        params['meeting'] = meeting
    def call(method, **values):
        return _result(srv._methods[method](1, values))
    created = call('groups.create', **params)['room']
    retry = call('groups.create', **params)['room']
    assert retry['idempotent'] is True
    assert created['meeting'] == retry['meeting'] == meeting
    listed = call('groups.list')['rooms'][0]
    state = call('groups.state', room_id=params['room_id'])['room']
    renamed = call('groups.rename', room_id=params['room_id'], event_id='rename', name='Renamed')['room']
    for room in (listed, state, renamed):
        assert room['meeting'] == meeting
    conflict = srv._methods['groups.create'](1, {
        **params, 'name': 'Renamed', 'meeting': {
            'kind': 'campfire', 'schedule_local': '21:00', 'timezone': 'UTC'}})
    assert conflict['error']['code'] == 4110
    assert call('groups.state', room_id=params['room_id'])['room']['meeting'] == meeting


def test_meeting_sends_share_one_thread_without_changing_ordinary_rooms(home):
    from tui_gateway import methods_groups
    service = methods_groups.get_hosted_room_service()
    assert service is not None
    assert service.stop(timeout=2)
    members = [{'member_id': 'one', 'profile': 'default', 'handle': 'one'},
               {'member_id': 'two', 'profile': 'ops', 'handle': 'two'}]
    for room_id, marker in [('meeting', {'kind': 'campfire', 'schedule_local': '21:00', 'timezone': 'UTC'}),
                            ('ordinary', None)]:
        service.create_room(room_id=room_id, name=room_id, members=members, meeting=marker)
        event = service.send(room_id=room_id, event_id='fixture-' + room_id,
                             payload={'text': 'Fixture question', 'thread_id': 'client-generated-thread'})
        assert event['payload']['thread_id'] == ('main' if marker else 'client-generated-thread')
