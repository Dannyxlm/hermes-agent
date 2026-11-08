"""Chat-open and tab sub-phases retain the recorder's bounded ingest contract."""
import json

import pytest

from tests.plugins.cloudseed_mobile.test_flight_recorder import (
    client as client, batch, P, T,  # noqa: PLC0414 - pytest fixture
)

SUBPHASES = (
    'ChatOpenToDestination', 'ChatOpenToModel', 'ChatOpenToTranscriptBody',
    'ChatOpenToTranscriptAppear', 'ChatOpenToPushEnd', 'TabToRootBody', 'TabToRootAppear',
)


def subphases(ms=10):
    return [
        {'kind': 'span', 't': T, 'name': name, 'outcome': 'ok', 'ms': ms,
         'attrs': {'entry': 'row'} if name.startswith('Chat') else {'from': '2', 'to': '3'}}
        for name in SUBPHASES
    ]


def test_subphase_batch_is_accepted_and_stored(client):
    c, root = client
    evs = subphases()
    response = c.post(P + '/batch', json=batch(evs=evs))
    assert response.status_code == 200
    assert response.json()['accepted'] == len(evs)
    assert response.json()['dropped_unknown_spans'] == 0
    assert response.json()['dropped_invalid_events'] == 0
    [day] = (root / 'events').iterdir()
    assert json.loads(day.read_text())['events'] == evs


@pytest.mark.parametrize('duration,accepted', [(0, True), (3_600_000, True), (-1, False),
                                               (3_600_001, False), (True, False)])
def test_subphases_preserve_duration_bounds(client, duration, accepted):
    c, root = client
    evs = subphases(ms=duration)
    response = c.post(P + '/batch', json=batch(evs=evs))
    assert response.status_code == 200
    assert response.json()['accepted'] == (len(evs) if accepted else 0)
    assert response.json()['dropped_invalid_events'] == (0 if accepted else len(evs))
    [day] = (root / 'events').iterdir()
    assert json.loads(day.read_text())['events'] == (evs if accepted else [])


@pytest.mark.parametrize('count,status', [(500, 200), (501, 400)])
def test_subphase_batch_preserves_event_cap(client, count, status):
    c, root = client
    evs = [subphases()[0] for _ in range(count)]
    response = c.post(P + '/batch', json=batch(evs=evs))
    assert response.status_code == status
    if status == 200:
        assert response.json()['accepted'] == count
        [day] = (root / 'events').iterdir()
        assert json.loads(day.read_text())['events'] == evs
    else:
        assert not (root / 'events').exists()
