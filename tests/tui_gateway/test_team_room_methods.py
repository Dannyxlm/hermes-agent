"""Team-room archive contracts through the real dispatcher and synthetic JSON only."""
import json
from pathlib import Path

import pytest

from tests.tui_gateway import test_methods_mobile as mobile
from tui_gateway import methods_team_room as room, server

pytestmark = pytest.mark.platforms("posix")

mobile_home = mobile.mobile_home
peer = mobile.peer
rpc = mobile.rpc


@pytest.fixture
def archive(mobile_home, peer, tmp_path, monkeypatch):
    (mobile_home / 'config.yaml').write_text(
        'cloudseed_mobile:\n  owner_provider: fixture-auth\n  owner_user_id: fixture-owner\n')
    peer.auth_identity = {'provider': 'fixture-auth', 'user_id': 'fixture-owner'}
    directory = tmp_path / 'meetings'
    directory.mkdir()
    monkeypatch.setenv('HERMES_TEAM_ROOM_DIR', str(directory))
    return directory


def write_meeting(archive, meeting_id='2026-11-02-standup', **changes):
    data = {'kind': meeting_id[11:], 'started': meeting_id[:10] + 'T08:15:00-05:00',
            'host': 'Ava', 'topic': None, 'dry_run': False,
            'sessions': {'Ava': 'synthetic-session-a', 'Iris': 'synthetic-session-i'},
            'messages': [{'name': 'Iris', 'text': '  Hello\n  synthetic room!  ', 'at': '08:16'},
                         {'name': 'Ava', 'text': '**Invented** reply\n', 'at': '08:17'}],
            'passes': [{'name': 'Iris', 'session': 'private-synthetic-pass'}],
            'kept': {'Iris': 'Invented lesson', 'Ava': 'nothing'}}
    data.update(changes)
    (archive / (meeting_id + '.json')).write_text(json.dumps(data))
    return data


def result(method='team_room.list', **params):
    response = rpc(method, **params)
    assert 'result' in response, response
    return response['result']


def invalid(method, **params):
    assert rpc(method, **params)['error'] == {'code': 4400, 'message': 'Invalid team room request'}


def test_ordering_exclusive_cursor_and_final_page(archive):
    # Timestamp order, not filename order or ISO text order; equal instants tie-break by id.
    ids = ['2026-10-30-campfire', '2026-11-02-standup', '2026-11-01-campfire', '2026-11-01-standup']
    starts = ['2026-11-02T10:00:00+00:00', '2026-11-02T08:00:00-05:00',
              '2026-11-01T21:00:00-05:00', '2026-11-01T21:00:00-05:00']
    for meeting_id, started in zip(ids, starts):
        write_meeting(archive, meeting_id, started=started)
    expected = [ids[1], ids[0], ids[3], ids[2]]
    first = result(limit=2)
    assert [m['id'] for m in first['meetings']] == expected[:2]
    assert first['next_before'] == expected[1]
    second = result(limit=2, before=first['next_before'])
    assert [m['id'] for m in second['meetings']] == expected[2:]
    assert second['next_before'] is None
    assert result(before=expected[-1]) == {'meetings': [], 'next_before': None}
    invalid('team_room.list', before='2020-01-01-standup')


def test_default_limit_and_bounds(archive):
    for day in range(1, 32):
        write_meeting(archive, f'2026-10-{day:02d}-standup')
    assert len(result()['meetings']) == 30
    assert len(result(limit=1)['meetings']) == 1
    assert len(result(limit=60)['meetings']) == 31
    assert result(before=None) == result()


@pytest.mark.parametrize('limit', [0, -1, 61, True, 1.5, '3', None])
def test_bad_limit(archive, limit):
    invalid('team_room.list', limit=limit)


@pytest.mark.parametrize('meeting_id', ['../x', '/2026-11-02-standup', '2026-11-02-standup.json',
                                       '2026-11-02-standup\n', '', '2026-11-02-other',
                                       '2026-11-02-standup/../x', '２０２６-１１-０２-standup', 1])
@pytest.mark.parametrize('method,key', [('team_room.get', 'id'), ('team_room.list', 'before')])
def test_reject_id_before_archive_io(archive, monkeypatch, meeting_id, method, key):
    def forbidden_archive():
        raise AssertionError('Invalid id must not touch the archive')
    monkeypatch.setattr(room, '_archive', forbidden_archive)
    invalid(method, **{key: meeting_id})


@pytest.mark.parametrize('method,params', [('team_room.get', {}), ('team_room.get', {'id': None}),
                                          ('team_room.get', {'id': '2026-11-02-standup', 'path': '../'}),
                                          ('team_room.list', {'unexpected': True})])
def test_missing_and_unknown_params(archive, method, params):
    invalid(method, **params)


def test_skip_dry_runs_invalid_names_and_malformed_files(archive):
    good = '2026-11-02-standup'
    write_meeting(archive, good)
    write_meeting(archive, '2026-11-01-campfire', dry_run=True)
    write_meeting(archive, '2026-10-31-campfire', messages='wrong shape')
    write_meeting(archive, '2026-10-30-campfire', started='not a timestamp')
    write_meeting(archive, '2026-10-29-campfire', kind='standup')
    write_meeting(archive, '2026-10-28-campfire', started='2026-10-28T21:00:00')
    (archive / '2026-10-27-campfire.json').write_text('{incomplete')
    (archive / '2026-10-26-campfire.json').write_bytes(b'\xff\xfe')
    (archive / '2026-10-25-campfire.json').write_text('null')
    (archive / '2026-11-03-standup-dryrun.json').write_text('{}')
    (archive / '2026-11-03-standup.md').write_text('ignored twin')
    assert [m['id'] for m in result()['meetings']] == [good]
    for meeting_id in ['2026-11-01-campfire', '2026-10-31-campfire', '2026-10-27-campfire', '2020-01-01-standup']:
        invalid('team_room.get', id=meeting_id)
        invalid('team_room.list', before=meeting_id)


def test_get_exact_projection_and_kept_participant_order(archive):
    messages = [{'name': 'Iris', 'text': '**Invented**\n  preserve whitespace  ', 'at': '08:16'}]
    data = write_meeting(archive, sessions={'Nuru': 's-n', 'Ava': 's-a', 'Iris': 's-i', 'Vox': 's-v', 'Matisse': 's-m'},
                         topic='Synthetic topic', messages=messages,
                         kept={'Iris': '  keep exactly  ', 'Ava': 'NOTHING', 'Nuru': 'Invented source',
                               'Vox': '', 'Matisse': ' \n ', 'Unknown': 'not a participant'})
    before = (archive / '2026-11-02-standup.json').read_bytes()
    assert result('team_room.get', id='2026-11-02-standup') == {
        'id': '2026-11-02-standup', 'kind': 'standup', 'date': '2026-11-02',
        'started': data['started'], 'host': 'Ava', 'topic': 'Synthetic topic',
        'participants': ['Nuru', 'Ava', 'Iris', 'Vox', 'Matisse'], 'messages': messages,
        'kept': [{'name': 'Nuru', 'text': 'Invented source'}, {'name': 'Iris', 'text': '  keep exactly  '}]}
    assert (archive / '2026-11-02-standup.json').read_bytes() == before
    assert sorted(p.name for p in archive.iterdir()) == ['2026-11-02-standup.json']


def test_summary_preview_and_participant_fallback(archive):
    write_meeting(archive, sessions={}, messages=[{'name': 'Iris', 'text': '  a\n b  ', 'at': '08:16'},
                                               {'name': 'Ava', 'text': 'x', 'at': '08:17'},
                                               {'name': 'Iris', 'text': 'y', 'at': '08:18'}])
    row = result()['meetings'][0]
    assert row['participants'] == ['Iris', 'Ava']
    assert row['message_count'] == 3 and row['preview'] == 'a b' and row['topic'] is None
    write_meeting(archive, messages=[{'name': 'Iris', 'text': 'x' * 141, 'at': '08:16'}])
    assert result()['meetings'][0]['preview'] == 'x' * 139 + '…'
    write_meeting(archive, messages=[{'name': 'Iris', 'text': 'x' * 140, 'at': '08:16'}])
    assert result()['meetings'][0]['preview'] == 'x' * 140
    write_meeting(archive, messages=[], sessions={})
    row = result()['meetings'][0]
    assert row['preview'] == '' and row['message_count'] == 0 and row['participants'] == []


@pytest.mark.parametrize('identity', [None, {'provider': 'foreign', 'user_id': 'intruder'},
                                       {'provider': 'internal', 'user_id': 'service'}])
@pytest.mark.parametrize('method,params', [('team_room.list', {}), ('team_room.get', {'id': '2026-11-02-standup'})])
def test_owner_only(archive, peer, identity, method, params):
    peer.auth_identity = identity
    assert rpc(method, **params)['error']['code'] == 4403


@pytest.mark.parametrize('method,params', [('team_room.list', {}), ('team_room.get', {'id': '2026-11-02-standup'})])
def test_missing_or_unreadable_archive(archive, monkeypatch, method, params):
    monkeypatch.setenv('HERMES_TEAM_ROOM_DIR', str(archive / 'absent'))
    assert rpc(method, **params)['error'] == {'code': 4401, 'message': 'Team room unavailable'}
    monkeypatch.setenv('HERMES_TEAM_ROOM_DIR', str(archive))
    def denied(*args, **kwargs):
        raise PermissionError('synthetic denial')
    monkeypatch.setattr(room, '_archive', denied)
    assert rpc(method, **params)['error'] == {'code': 4401, 'message': 'Team room unavailable'}


def test_symlink_containment_and_non_regular_files(archive, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    write_meeting(outside)
    escaped = '2026-11-02-standup'
    (archive / (escaped + '.json')).symlink_to(outside / (escaped + '.json'))
    invalid('team_room.get', id=escaped)
    assert result()['meetings'] == []
    good = '2026-11-01-campfire'
    write_meeting(archive, good)
    # In-root aliases are safe; replacing a checked component cannot escape at open time.
    (archive / '2026-10-31-campfire.json').symlink_to(archive / (good + '.json'))
    assert result('team_room.get', id='2026-10-31-campfire')['kind'] == 'campfire'
    (archive / '2026-10-30-campfire.json').symlink_to(archive)
    (archive / '2026-10-29-campfire.json').mkdir()
    invalid('team_room.get', id='2026-10-30-campfire')
    invalid('team_room.get', id='2026-10-29-campfire')
    assert len(result()['meetings']) == 2


def test_oversized_file_and_scan_caps(archive, monkeypatch):
    write_meeting(archive)
    huge = archive / '2026-11-01-campfire.json'
    with huge.open('wb') as stream:
        stream.truncate(room.MAX_FILE_BYTES + 1)
    assert len(result()['meetings']) == 1
    invalid('team_room.get', id=huge.stem)
    monkeypatch.setattr(room, 'MAX_MEETING_FILES', 1)
    assert rpc('team_room.list')['error']['code'] == 4401
    monkeypatch.setattr(room, 'MAX_MEETING_FILES', 512)
    monkeypatch.setattr(room, 'MAX_DIRECTORY_ENTRIES', 1)
    assert rpc('team_room.list')['error']['code'] == 4401


def test_default_owner_home_archive(archive, tmp_path, monkeypatch):
    directory = tmp_path / 'hermes-workspaces/Automation/team-room/meetings'
    directory.mkdir(parents=True)
    write_meeting(directory)
    monkeypatch.delenv('HERMES_TEAM_ROOM_DIR')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert result()['meetings'][0]['id'] == '2026-11-02-standup'


def test_additive_archive_metadata_is_not_exposed(archive):
    data = write_meeting(archive, internal_note='invented private metadata')
    data['messages'][0]['session_id'] = 'synthetic-private-id'
    (archive / '2026-11-02-standup.json').write_text(json.dumps(data))
    snapshot = result('team_room.get', id='2026-11-02-standup')
    assert 'internal_note' not in snapshot
    assert set(snapshot['messages'][0]) == {'name', 'text', 'at'}
    # The optional domain error override leaves all other RPCs' unknown-key code unchanged.
    assert rpc('mobile.capabilities', unexpected=True)['error']['code'] == 4000


@pytest.mark.parametrize('replace_parent', [False, True])
def test_symlink_swap_after_resolution_cannot_escape(archive, tmp_path, monkeypatch, replace_parent):
    outside = tmp_path / 'outside'
    outside.mkdir()
    write_meeting(outside)
    meeting_id = '2026-11-02-standup'
    candidate = archive / (meeting_id + '.json')
    nested = archive / 'nested'
    if replace_parent:
        nested.mkdir()
        write_meeting(nested)
        candidate.symlink_to(nested / candidate.name)
    else:
        write_meeting(archive)
    resolve = Path.resolve
    swapped = False
    def swap_after_check(path, *args, **kwargs):
        nonlocal swapped
        resolved = resolve(path, *args, **kwargs)
        if path == candidate and not swapped:
            swapped = True
            if replace_parent:
                nested.rename(archive / 'retired')
                nested.symlink_to(outside, target_is_directory=True)
            else:
                candidate.unlink()
                candidate.symlink_to(outside / candidate.name)
        return resolved
    monkeypatch.setattr(Path, 'resolve', swap_after_check)
    invalid('team_room.get', id=meeting_id)
    assert swapped


def test_mobile_capabilities_advertise_callable_archive_methods(archive):
    write_meeting(archive)
    advertised = result('mobile.capabilities')['methods']
    assert {'team_room.list', 'team_room.get'} <= set(advertised)
    assert result()['meetings']
    assert result('team_room.get', id='2026-11-02-standup')['messages']
    assert {'team_room.list', 'team_room.get'} <= server._LONG_HANDLERS
