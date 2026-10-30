"""Future span names cannot silence a batch's known telemetry."""
import json

import pytest

from tests.plugins.cloudseed_mobile.test_flight_recorder import (
    client as client, assert_event_dropped, batch, events, flag, P, T,  # noqa: PLC0414 - pytest fixture
)

NEW_SPANS = ('app.foreground_fresh', 'chat.history_complete', 'chat.send_first_delta', 'InboxFreshAfterForeground',
             'ChatOpenToBody', 'ChatOpenBodyToTranscript', 'ChatOpenDocParse')


def span(name):
    return {'kind': 'span', 't': T, 'name': name, 'outcome': 'ok', 'ms': 10,
            'attrs': {'cached': 'true'}}


@pytest.mark.parametrize('name', NEW_SPANS)
def test_new_app_span_is_stored_with_co_batched_events(client, name):
    c, root = client
    evs = [events()[0], span(name), events()[3]]
    response = c.post(P + '/batch', json=batch(evs=evs))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == len(evs)
    [day] = (root / 'events').iterdir()
    assert json.loads(day.read_text())['events'] == evs


@pytest.mark.parametrize('source', ['banner', 'live_activity', 'url', 'widget', 'attention_row'])
@pytest.mark.parametrize('launch', ['cold', 'warm', 'foreground'])
@pytest.mark.parametrize('tail', ['memory', 'disk', 'none'])
def test_notification_open_span_contract_through_ingest(client, source, launch, tail):
    c, root = client
    event = span('NotificationOpenToContent') | {'attrs': {'source': source, 'launch': launch, 'tail': tail}}
    response = c.post(P + '/batch', json=batch(evs=[events()[0], event]))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == 2
    [day] = (root / 'events').iterdir()
    assert json.loads(day.read_text())['events'] == [events()[0], event]


@pytest.mark.parametrize('source', ['head', 'delta'])
def test_inbox_fresh_span_keeps_its_source_attribute(client, source):
    c, root = client
    event = span('InboxFreshAfterForeground') | {'attrs': {'source': source}}
    response = c.post(P + '/batch', json=batch(evs=[events()[0], event]))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == 2
    [day] = (root / 'events').iterdir()
    assert json.loads(day.read_text())['events'] == [events()[0], event]


@pytest.mark.parametrize('outcome', ['ok', 'cancelled', 'abandoned', 'superseded'])
def test_notification_open_span_keeps_existing_outcomes(client, outcome):
    c, root = client
    event = span('NotificationOpenToContent') | {'outcome': outcome}
    response = c.post(P + '/batch', json=batch(evs=[event]))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == 1
    [day] = (root / 'events').iterdir()
    assert json.loads(day.read_text())['events'] == [event]


@pytest.mark.parametrize('name', ['future.private_name', 'future span free text', 'x' * 100])
def test_future_span_drops_only_itself_and_counts_without_content(client, caplog, name):
    c, root = client
    future = span(name) | {'future_attribute': 'private dropped value'}
    known = [events()[0], events()[3], span('ChatOpenContent')]
    body = batch(evs=[known[0], future, *known[1:]])
    first = c.post(P + '/batch', json=body)
    assert first.status_code == 200, first.text
    assert first.json()['accepted'] == len(known)
    assert first.json()['dropped_unknown_spans'] == 1
    second = c.post(P + '/batch', json=body)
    assert second.status_code == 200 and second.json()['duplicate'] is True
    assert second.json()['accepted'] == 0
    assert second.json()['receipt_id'] == first.json()['receipt_id']
    [day] = (root / 'events').iterdir()
    lines = day.read_text().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored['events'] == known
    assert stored['dropped_unknown_spans'] == 1
    assert future['name'] not in day.read_text() + caplog.text + first.text
    assert c.post(P + '/batch', json=batch(evs=[known[0]])).status_code == 409


def test_all_unknown_batch_is_acknowledged_and_counted(client):
    c, root = client
    response = c.post(P + '/batch', json=batch(evs=[span('FutureOne'), span('FutureTwo')]))
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] == 0
    assert response.json()['dropped_unknown_spans'] == 2
    [day] = (root / 'events').iterdir()
    stored = json.loads(day.read_text())
    assert stored['events'] == [] and stored['dropped_unknown_spans'] == 2


def test_flag_event_bundle_has_same_unknown_span_isolation(client):
    c, root = client
    known = events()[:2]
    body = flag(shot=None, events=[span('FutureFlag'), *known])
    response = c.post(P + '/flag', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['dropped_unknown_spans'] == 1
    [record] = (root / 'flags').glob('*/*/flag.json')
    stored = json.loads(record.read_text())
    assert stored['events'] == known and stored['dropped_unknown_spans'] == 1
    assert 'FutureFlag' not in record.read_text()


@pytest.mark.parametrize('key,value', [
    ('source', 'spotlight'), ('source', 'Banner'), ('source', ''), ('source', None),
    ('launch', 'background'), ('launch', 'Cold'), ('launch', 1), ('launch', {'private': True}),
])
def test_notification_open_dimensions_drop_unknown_values(client, key, value):
    c, root = client
    event = span('NotificationOpenToContent') | {'attrs': {key: value}}
    assert_event_dropped(c, root, [events()[0], event], [events()[0]])


@pytest.mark.parametrize('invalid', [
    {'kind': 'future_event', 't': T, 'private_key': 'PRIVATE_DROPPED_CONTENT'},
    span('ChatOpenContent') | {'attrs': {'future_attr': 'PRIVATE_DROPPED_CONTENT'}},
    {'kind': ['PRIVATE_DROPPED_CONTENT'], 't': T},
    {'kind': 'live_activity', 't': T, 'action': {'private': 'PRIVATE_DROPPED_CONTENT'}, 'origin': 'local'},
    'PRIVATE_DROPPED_CONTENT',
])
def test_invalid_event_isolation_is_content_free_and_idempotent(client, caplog, invalid):
    c, root = client
    known = events()[:2]
    body = batch(evs=[known[0], invalid, known[1]])
    first = c.post(P + '/batch', json=body)
    assert first.status_code == 200, first.text
    assert first.json()['accepted'] == 2
    assert first.json()['dropped_invalid_events'] == 1
    again = c.post(P + '/batch', json=body)
    assert again.status_code == 200
    assert again.json()['duplicate'] is True and again.json()['accepted'] == 0
    assert again.json()['receipt_id'] == first.json()['receipt_id']
    [day] = (root / 'events').iterdir()
    assert len(day.read_text().splitlines()) == 1
    record = json.loads(day.read_text())
    assert record['events'] == known and record['dropped_invalid_events'] == 1
    assert 'PRIVATE_DROPPED_CONTENT' not in day.read_text() + caplog.text + first.text + again.text
    assert c.post(P + '/batch', json=batch(evs=known[:1])).status_code == 409


def test_flag_keeps_known_events_when_one_event_has_an_unknown_attribute(client):
    c, root = client
    known = events()[:2]
    invalid = span('ChatOpenContent') | {'attrs': {'private_key': 'PRIVATE_DROPPED_CONTENT'}}
    body = flag(shot=None, events=[known[0], invalid, known[1]])
    first = c.post(P + '/flag', json=body)
    assert first.status_code == 200, first.text
    assert first.json()['dropped_invalid_events'] == 1
    again = c.post(P + '/flag', json=body)
    assert again.status_code == 200 and again.json()['duplicate'] is True
    [record] = (root / 'flags').glob('*/*/flag.json')
    stored = json.loads(record.read_text())
    assert stored['events'] == known and stored['dropped_invalid_events'] == 1
    assert 'PRIVATE_DROPPED_CONTENT' not in record.read_text() + first.text


@pytest.mark.parametrize('change', [
    {'outcome': 'future'}, {'attrs': {'not_allowed': 'true'}},
    {'name': None}, {'name': {'unhashable': True}},
])
def test_known_schema_validation_drops_only_the_invalid_event(client, change):
    c, root = client
    invalid = span('ChatOpenContent') | change
    assert_event_dropped(c, root, [events()[0], invalid], [events()[0]])
