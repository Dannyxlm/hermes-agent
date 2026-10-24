"""Future span names cannot silence a batch's known telemetry."""
import json

import pytest

from tests.plugins.cloudseed_mobile.test_flight_recorder import (
    client as client, batch, events, flag, P, T,  # noqa: PLC0414 - pytest fixture
)

NEW_SPANS = ('app.foreground_fresh', 'chat.history_complete', 'chat.send_first_delta')


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


def test_future_span_drops_only_itself_and_counts_without_content(client, caplog):
    c, root = client
    future = span('future.private_name')
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


@pytest.mark.parametrize('change', [
    {'outcome': 'future'}, {'attrs': {'not_allowed': 'true'}},
    {'name': 'arbitrary free text'}, {'name': {'unhashable': True}},
])
def test_known_schema_validation_is_not_relaxed(client, change):
    c, _ = client
    invalid = span('ChatOpenContent') | change
    assert c.post(P + '/batch', json=batch(evs=[events()[0], invalid])).status_code == 400
