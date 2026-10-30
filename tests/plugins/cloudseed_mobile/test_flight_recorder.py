"""Flight recorder contracts on synthetic data; never opens production stores."""
import base64
import datetime as dt
import json
import os
import stat
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from plugins.cloudseed_mobile import flight_recorder as fr
from plugins.cloudseed_mobile.dashboard import plugin_api as api

P = '/api/plugins/cloudseed_mobile/flight-recorder'
INSTALL = str(uuid.UUID('7a1c0de5-0000-4000-8000-00000000abcd'))
OTHER = str(uuid.UUID(int=8))
T = 1_791_400_000_000
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 64 + b'\xff\xd9'
SENTINEL = 'Example pricing hi'


@pytest.fixture
def client(monkeypatch, tmp_path):
    home = tmp_path
    monkeypatch.setenv('HERMES_HOME', str(home))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, '_get_platform_default_hermes_home', lambda: home)
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'owner', 'owner_provider': 'basic'})
    fr._last_prune.clear()
    app = FastAPI()

    @app.middleware('http')
    async def session(request, call_next):
        if request.headers.get('authorization') == 'Bearer fixture':
            request.state.session = SimpleNamespace(user_id='owner', provider='basic')
        return await call_next(request)
    from hermes_cli.web_server_dashboard import _plugin_route_secret_scope
    app.include_router(api.router, prefix='/api/plugins/cloudseed_mobile', dependencies=[Depends(_plugin_route_secret_scope)])
    with TestClient(app) as c:
        c.headers['Authorization'] = 'Bearer fixture'
        yield c, home / 'mobile' / 'flight-recorder'


def events():
    return [
        {'kind': 'lifecycle', 't': T, 'state': 'launch'},
        {'kind': 'screen', 't': T + 1, 'screen': 'chat', 'session_id': '20261007_195052_eff4cd'},
        {'kind': 'tap', 't': T + 2, 'control': 'composer.send', 'screen': 'chat'},
        {'kind': 'rpc', 't': T + 3, 'method': 'prompt.submit', 'outcome': 'ok', 'ms': 41.5, 'bytes': 220,
         'session_id': '20261007_195052_eff4cd'},
        {'kind': 'http', 't': T + 4, 'route': '/api/plugins/cloudseed_mobile/deliverables', 'verb': 'GET',
         'outcome': 'error', 'ms': 900, 'status': 503},
        {'kind': 'error', 't': T + 5, 'domain': 'url', 'code': -1001, 'screen': 'inbox'},
        {'kind': 'span', 't': T + 6, 'name': 'ChatOpenContent', 'outcome': 'ok', 'ms': 180},
        {'kind': 'connection', 't': T + 7, 'state': 'disconnected', 'transport': 'ws', 'code': 1006},
        {'kind': 'metrickit', 't': T + 8, 'payload_type': 'diagnostic', 'payload': {'hangDiagnostics': []}},
    ]


def batch(seq=0, install=INSTALL, evs=None, **extra):
    return {'profile': 'default', 'schema': 1, 'install_id': install, 'batch_seq': seq, 'app_build': '2026100705',
            'os_version': '27.0', 'device_class': 'iPhone18,1', 'events': evs if evs is not None else events(), **extra}


def flag(flag_id=None, install=INSTALL, shot=JPEG, **extra):
    body = {'profile': 'default', 'schema': 1, 'install_id': install, 'flag_id': flag_id or str(uuid.uuid4()),
            't': T, 'screen': 'chat', 'note': 'the composer jumped', 'app_build': '2026100705', 'os_version': '27.0',
            'device_class': 'iPhone18,1', 'events': events()[:3], **extra}
    if shot is not None:
        body['screenshot_jpeg_b64'] = base64.b64encode(shot).decode()
    return body


def assert_event_dropped(c, root, evs, known, seq=0):
    response = c.post(P + '/batch', json=batch(seq=seq, evs=evs))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == len(known)
    assert response.json()['dropped_invalid_events'] == 1
    [day] = (root / 'events').iterdir()
    record = json.loads(day.read_text().splitlines()[-1])
    assert record['events'] == known
    assert record['dropped_invalid_events'] == 1


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_batch_is_stored_privately_with_receipt(client):
    c, root = client
    response = c.post(P + '/batch', json=batch())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['accepted'] == 9 and body['duplicate'] is False and len(body['receipt_id']) == 24
    files = list((root / 'events').iterdir())
    assert len(files) == 1 and files[0].name.endswith('.jsonl')
    record = json.loads(files[0].read_text())
    assert record['install_id'] == INSTALL and record['profile'] == 'default' and len(record['events']) == 9
    assert mode(files[0]) == 0o600 and mode(root / 'index.sqlite3') == 0o600
    assert mode(root) == mode(root / 'events') == mode(root.parent) == 0o700


def test_duplicate_batch_is_idempotent_and_reuse_conflicts(client):
    c, root = client
    first = c.post(P + '/batch', json=batch()).json()
    again = c.post(P + '/batch', json=batch()).json()
    assert again == {**first, 'accepted': 0, 'duplicate': True}
    assert c.post(P + '/batch', json=batch(evs=events()[:2])).status_code == 409
    [day] = (root / 'events').iterdir()
    assert len(day.read_text().splitlines()) == 1


@pytest.mark.parametrize('index,key', [
    (1, 'screen'), (2, 'control'), (3, 'method'), (3, 'session_id'), (4, 'route'), (5, 'domain'),
])
def test_free_text_cannot_ride_in_any_event_field(client, index, key):
    c, root = client
    evs = events()
    evs[index][key] = SENTINEL
    assert_event_dropped(c, root, evs, [e for i, e in enumerate(events()) if i != index])


@pytest.mark.parametrize('mutate', [
    lambda e: e[0].update(note='hello'),                       # unknown key
    lambda e: e[0].update(kind='keystroke'),                   # unknown kind
    lambda e: e[3].update(ms=True),                            # bool is not a number
    lambda e: e[3].update(ms=-1),
    lambda e: e[3].update(outcome='weird'),
    lambda e: e[4].update(route='/api/x?q=secret'),            # query strings never allowed
    lambda e: e[0].pop('t'),
    lambda e: e[8].update(payload={'blob': 'x' * (fr.MAX_METRICKIT + 1)}),
])
def test_invalid_events_are_dropped_without_blinding_the_batch(client, mutate):
    c, root = client
    evs = events()
    mutate(evs)
    assert_event_dropped(c, root, evs, [e for e in events() if e in evs])


def test_envelope_bounds(client):
    c, _ = client
    assert c.post(P + '/batch', json=batch(extra_field=1)).status_code == 400
    assert c.post(P + '/batch', json=batch(evs=[])).status_code == 400
    assert c.post(P + '/batch', json=batch(evs=events()[:1] * (fr.MAX_BATCH_EVENTS + 1))).status_code in (400, 413)
    assert c.post(P + '/batch', json=batch(install='not-a-uuid')).status_code == 400
    assert c.post(P + '/batch', json=batch(install='{' + INSTALL + '}')).status_code == 400
    assert c.post(P + '/batch', json=batch(install=INSTALL.replace('-', ''))).status_code == 400
    # Swift's uuidString is upper-case: accept it, normalise, and dedupe across case.
    assert c.post(P + '/batch', json=batch(install=INSTALL.upper())).json()['duplicate'] is False
    assert c.post(P + '/batch', json=batch()).json()['duplicate'] is True
    assert c.post(P + '/batch', json={**batch(), 'schema': 2}).status_code == 400


def test_owner_and_profile_are_required(client, monkeypatch):
    c, _ = client
    assert c.post(P + '/batch', json={**batch(), 'profile': 'b'}).status_code == 403
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'another', 'owner_provider': 'basic'})
    assert c.post(P + '/batch', json=batch()).status_code == 403
    c.headers.pop('Authorization')
    assert c.post(P + '/batch', json=batch()).status_code == 401
    assert c.post(P + '/flag', json=flag()).status_code == 401
    assert c.post(P + '/delete', json={'profile': 'default', 'install_id': INSTALL}).status_code == 401


def test_flag_stores_screenshot_and_note_privately(client):
    c, root = client
    flag_id = str(uuid.uuid4())
    response = c.post(P + '/flag', json=flag(flag_id))
    assert response.status_code == 200, response.text
    [folder] = (root / 'flags').glob(f'*/{flag_id}')
    assert (folder / 'screenshot.jpg').read_bytes() == JPEG
    record = json.loads((folder / 'flag.json').read_text())
    assert record['note'] == 'the composer jumped' and record['has_screenshot'] is True
    assert 'screenshot_jpeg_b64' not in record
    assert mode(folder) == 0o700 and mode(folder / 'flag.json') == mode(folder / 'screenshot.jpg') == 0o600
    assert c.post(P + '/flag', json=flag(flag_id)).json()['duplicate'] is True
    assert c.post(P + '/flag', json=flag(flag_id, note='changed')).status_code == 409


def test_flag_without_screenshot_and_bad_images(client):
    c, _root = client
    assert c.post(P + '/flag', json=flag(shot=None)).status_code == 200
    assert c.post(P + '/flag', json=flag(shot=b'\x89PNG\r\n\x1a\n')).status_code == 400
    assert c.post(P + '/flag', json=flag(shot=None, screenshot_jpeg_b64='***')).status_code == 400
    assert c.post(P + '/flag', json=flag(note='x' * (fr.MAX_NOTE + 1))).status_code == 400


def test_delete_removes_only_that_installation(client):
    c, root = client
    assert c.post(P + '/batch', json=batch(0)).status_code == 200
    assert c.post(P + '/batch', json=batch(0, install=OTHER)).status_code == 200
    assert c.post(P + '/flag', json=flag()).status_code == 200
    kept_flag = str(uuid.uuid4())
    assert c.post(P + '/flag', json=flag(kept_flag, install=OTHER)).status_code == 200
    response = c.post(P + '/delete', json={'profile': 'default', 'install_id': INSTALL})
    assert response.json() == {'ok': True, 'removed_batches': 1, 'removed_flags': 1}
    [day] = (root / 'events').iterdir()
    assert [json.loads(line)['install_id'] for line in day.read_text().splitlines()] == [OTHER]
    assert mode(day) == 0o600
    assert [p.name for p in (root / 'flags').glob('*/*')] == [kept_flag]
    # The same sequence can be uploaded again after a delete.
    assert c.post(P + '/batch', json=batch(0)).json()['duplicate'] is False
    assert c.post(P + '/delete', json={'profile': 'default', 'install_id': INSTALL, 'x': 1}).status_code == 400


def test_storage_cap_returns_507(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(fr, 'MAX_DAY_FILE', 100)
    response = c.post(P + '/batch', json=batch())
    assert response.status_code == 507 and response.json()['code'] == 'storage_full'


def test_symlinked_store_is_refused(client, tmp_path_factory):
    c, root = client
    elsewhere = tmp_path_factory.mktemp('elsewhere')
    root.parent.mkdir(mode=0o700)
    root.symlink_to(elsewhere, target_is_directory=True)
    assert c.post(P + '/batch', json=batch()).status_code == 400
    assert list(elsewhere.iterdir()) == []


def test_prune_removes_only_old_dated_entries(tmp_path):
    now = dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.UTC)
    store = fr.FlightRecorderStore(tmp_path / 'mobile' / 'flight-recorder', now=lambda: now)
    store.put_batch('default', fr.validate_batch(batch()))
    events_dir, flags_dir = store.root / 'events', store.root / 'flags'
    (events_dir / '2026-08-01.jsonl').write_text('{}\n')
    (events_dir / 'notes.txt').write_text('keep')
    (flags_dir / '2026-08-01' / 'x').mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious').write_text('keep')
    (flags_dir / '2026-08-02').symlink_to(outside, target_is_directory=True)
    assert store.prune() == 2
    assert sorted(p.name for p in events_dir.iterdir()) == ['2026-10-07.jsonl', 'notes.txt']
    assert (outside / 'precious').exists()


# ---- Round 9 U37: closed allowlists for R53 kinds, span dimensions and MetricKit keys ----
# Mirrors HermesMobile/Diagnostics/FlightRecorderEvent.swift on feat/hx-r9-flight-recorder-20261007 (b5afd80).

SESSION = '20261007_195052_eff4cd'


def legacy_span_attrs():
    return {key: 'true' for key in (
        'cached', 'editing_begins', 'entry', 'from', 'host_ready', 'inbox_rows', 'keyboard_shown',
        'keyboard_was_visible', 'loading', 'notice', 'pre_main_ms', 'restored', 'rows',
        'should_begin_calls', 'state', 'surface', 'to', 'verified_empty')}


def r53_events():
    attrs = legacy_span_attrs()
    # Keep legacy event positions stable for the malformed-dimension matrix below.
    names = fr.SPAN_NAMES - {'app.foreground_fresh', 'chat.history_complete', 'chat.send_first_delta',
                             'NotificationOpenToContent'}
    spans = [{'kind': 'span', 't': T + i, 'name': name, 'outcome': outcome, 'ms': 120, 'screen': 'inbox',
              'attrs': attrs}
             for i, (name, outcome) in enumerate(zip(sorted(names), sorted(fr.SPAN_OUTCOMES)))]
    return spans + [
        {'kind': 'span', 't': T + 20, 'name': 'ScrollInteraction', 'outcome': 'superseded', 'ms': 3},
        {'kind': 'bg_refresh', 't': T + 21, 'trigger': 'silent_push', 'outcome': 'new_data', 'ms': 812},
        {'kind': 'bg_refresh', 't': T + 22, 'trigger': 'scheduled', 'outcome': 'expired', 'ms': 30000},
        {'kind': 'push', 't': T + 23, 'push_type': 'live_activity', 'app_state': 'suspended', 'session_id': SESSION},
        {'kind': 'push', 't': T + 24, 'push_type': 'background', 'app_state': 'background'},
        {'kind': 'push_tap', 't': T + 25, 'source': 'attention_row', 'outcome': 'foreign_account', 'session_id': SESSION},
        {'kind': 'live_activity', 't': T + 26, 'action': 'start', 'origin': 'push_to_start', 'session_id': SESSION},
        {'kind': 'live_activity', 't': T + 27, 'action': 'end', 'origin': 'local', 'end_state': 'dismissed',
         'session_id': '7A1C0DE5-0000-4000-8000-00000000ABCD'},
    ]


def test_r53_kinds_and_span_dimensions_are_accepted(client):
    c, root = client
    evs = r53_events()
    response = c.post(P + '/batch', json=batch(evs=evs))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == len(evs)
    [day] = (root / 'events').iterdir()
    stored = json.loads(day.read_text())['events']
    assert {e['kind'] for e in stored} == {'span', 'bg_refresh', 'push', 'push_tap', 'live_activity'}
    assert {e['outcome'] for e in stored if e['kind'] == 'span'} == fr.SPAN_OUTCOMES
    assert stored[0]['attrs'] == legacy_span_attrs()


def test_allowlists_match_the_app_schema_exactly():
    assert fr.SPAN_NAMES == {'LaunchInboxContent', 'ResumeInboxContent', 'ChatOpenContent',
                             'ComposerFocusToSettled', 'TabFirstContent', 'ScrollInteraction',
                             'app.foreground_fresh', 'chat.history_complete', 'chat.send_first_delta',
                             'NotificationOpenToContent', 'InboxFreshAfterForeground', 'ChatOpenToBody',
                             'ChatOpenBodyToTranscript', 'ChatOpenDocParse'}
    assert fr.SPAN_OUTCOMES == {'ok', 'cancelled', 'abandoned', 'superseded', 'empty_verified', 'no_rows', 'no_keyboard'}
    assert fr.SCREENS == {'inbox', 'chats', 'chat', 'desktop', 'files', 'file_preview', 'settings', 'sign_in',
                          'flag', 'unknown'}
    assert fr.ERROR_DOMAINS == {'url', 'posix', 'cocoa', 'transport', 'auth', 'http', 'decoding', 'rpc', 'app'}
    assert set(fr.KINDS) == {'lifecycle', 'screen', 'tap', 'rpc', 'http', 'error', 'span', 'connection',
                             'metrickit', 'bg_refresh', 'push', 'push_tap', 'live_activity'}


@pytest.mark.parametrize('index,mutation', [
    (0, {'kind': 'bg_refresh_v2'}),
    (7, {'trigger': 'manual'}),
    (7, {'outcome': 'partial'}),
    (9, {'push_type': 'voip'}),
    (9, {'title': 'hello'}),
    (11, {'source': 'spotlight'}),
    (12, {'origin': 'remote'}),
    (13, {'end_state': 'gone'}),
    (0, {'outcome': 'weird'}),
    (0, {'attrs': {'title': 'true'}}),
    (0, {'attrs': {'state': SENTINEL}}),
    (0, {'attrs': {'state': {'nested': 'x'}}}),
    (0, {'attrs': ['state']}),
    (0, {'screen': 'compose'}),
    (9, {'session_id': 'example-pricing-notes'}),
])
def test_r53_unknown_kinds_values_and_keys_drop_only_the_event(client, index, mutation):
    c, root = client
    evs = r53_events()
    evs[index].update(mutation)
    assert_event_dropped(c, root, evs, [e for i, e in enumerate(r53_events()) if i != index])


def test_error_domain_and_screen_are_closed_vocabularies(client):
    c, root = client
    for seq, mutate in enumerate((lambda e: e[5].update(domain='NSURLErrorDomain'),
                                  lambda e: e[1].update(screen='Compose'))):
        evs = events()
        mutate(evs)
        assert_event_dropped(c, root, evs, [e for e in events() if e in evs], seq=seq)


def test_metrickit_keeps_only_fixed_keys_and_strips_free_text(client):
    c, root = client
    payload = {
        'timeStampBegin': '2026-10-07 00:00', 'metaData': {'appBuildVersion': '2026100705', 'bundleIdentifier': 'x'},
        'crashDiagnostics': [{'diagnosticMetaData': {'terminationReason': SENTINEL, 'exceptionType': 1,
                                                     'composedMessage': SENTINEL, 'signal': 11},
                              'callStackTree': {'frames': ['f' * 300]}}],
        'customKey': SENTINEL,
    }
    evs = [{'kind': 'metrickit', 't': T, 'payload_type': 'diagnostic', 'payload': payload}]
    assert c.post(P + '/batch', json=batch(evs=evs)).status_code == 200
    [day] = (root / 'events').iterdir()
    text = day.read_text()
    stored = json.loads(text)['events'][0]['payload']
    assert SENTINEL not in text and 'customKey' not in stored
    assert stored['metaData'] == {'appBuildVersion': '2026100705'}
    crash = stored['crashDiagnostics'][0]
    assert crash['diagnosticMetaData'] == {'exceptionType': 1, 'signal': 11}
    assert crash['callStackTree'] == {'frames': ['[removed]']}


def test_metrickit_summary_form_keeps_only_summary_keys(client):
    c, root = client
    summary = {'hermex_summary': True, 'original_bytes': 400000, 'timeStampEnd': '2026-10-07 01:00',
               'counts': {'hangDiagnostics': 3, 'customKey': 9, 'crashDiagnostics': 'many'},
               'hangDiagnostics': [{'x': 1}], 'note': SENTINEL}
    evs = [{'kind': 'metrickit', 't': T, 'payload_type': 'diagnostic', 'payload': summary}]
    assert c.post(P + '/batch', json=batch(evs=evs)).status_code == 200
    [day] = (root / 'events').iterdir()
    stored = json.loads(day.read_text())['events'][0]['payload']
    assert stored == {'hermex_summary': True, 'original_bytes': 400000, 'timeStampEnd': '2026-10-07 01:00',
                      'counts': {'hangDiagnostics': 3}}


def test_metrickit_summary_drops_a_non_integer_size(client):
    c, root = client
    summary = {'hermex_summary': True, 'original_bytes': SENTINEL, 'counts': {}}
    evs = [{'kind': 'metrickit', 't': T, 'payload_type': 'metric', 'payload': summary}]
    assert c.post(P + '/batch', json=batch(evs=evs)).status_code == 200
    [day] = (root / 'events').iterdir()
    assert SENTINEL not in day.read_text()


@pytest.mark.parametrize('action', ['start', 'end', 'token_registered', 'end_requested'])
@pytest.mark.parametrize('origin', ['local', 'push_to_start'])
@pytest.mark.parametrize('act', [None, '0123abcd'])
def test_live_activity_lifecycle_contract_is_stored_through_ingest(client, action, origin, act):
    c, root = client
    event = {'kind': 'live_activity', 't': T, 'action': action, 'origin': origin}
    if act is not None:
        event['act'] = act
    response = c.post(P + '/batch', json=batch(evs=[event]))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == 1
    [day] = (root / 'events').iterdir()
    assert json.loads(day.read_text())['events'] == [event]


@pytest.mark.parametrize('action', ['start', 'end', 'token_registered', 'end_requested'])
def test_live_activity_null_correlation_is_optional(client, action):
    c, root = client
    event = {'kind': 'live_activity', 't': T, 'action': action, 'origin': 'local', 'act': None}
    response = c.post(P + '/batch', json=batch(evs=[event]))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == 1
    [day] = (root / 'events').iterdir()
    assert json.loads(day.read_text())['events'] == [event]


@pytest.mark.parametrize('act', ['', '0123abc', '0123abcde', '0123ABCD', 'not-hex!', 12345678, {'id': 'private'}])
def test_live_activity_correlation_refuses_non_digest_values(client, act):
    c, root = client
    event = {'kind': 'live_activity', 't': T, 'action': 'start', 'origin': 'local', 'act': act}
    assert_event_dropped(c, root, [events()[0], event], [events()[0]])


@pytest.mark.parametrize('tail', ['memory', 'disk', 'none'])
def test_keyboard_and_tail_attrs_ingest_without_changing_legacy_batches(client, tail):
    c, root = client
    evs = [
        {'kind': 'span', 't': T, 'name': 'ComposerFocusToSettled', 'outcome': 'ok', 'ms': 300,
         'attrs': {'will_show_ms': '0', 'did_show_ms': '600000', 'kb_anim_ms': '250'}},
        {'kind': 'span', 't': T + 1, 'name': 'ChatOpenContent', 'outcome': 'ok', 'ms': 30,
         'attrs': {'tail': tail}},
    ]
    response = c.post(P + '/batch', json=batch(evs=evs))
    assert response.status_code == 200
    assert response.json()['accepted'] == 2
    assert c.post(P + '/batch', json=batch(seq=1, evs=r53_events())).status_code == 200
    [day] = (root / 'events').iterdir()
    records = [json.loads(line) for line in day.read_text().splitlines()]
    assert records[0]['events'] == evs
    assert records[1]['events'][0]['attrs'] == legacy_span_attrs()


@pytest.mark.parametrize('key,value', [
    ('tail', 'other'), ('tail', 'Memory'), ('tail', ''), ('tail', 0),
    *[(key, value) for key in ('will_show_ms', 'did_show_ms', 'kb_anim_ms')
      for value in ('-1', '600001', '1.5', '1e2', '+1', 'true', '١', '', 1, True, None)],
])
def test_new_span_attrs_reject_invalid_values_without_ingest(client, key, value):
    c, root = client
    evs = [{'kind': 'span', 't': T, 'name': 'ChatOpenContent', 'outcome': 'ok', 'ms': 30,
            'attrs': {key: value}}]
    assert_event_dropped(c, root, evs, [])
