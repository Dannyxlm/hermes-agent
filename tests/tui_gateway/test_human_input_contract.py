"""Canonical producer → registry → scoped action invariants (dummy input only)."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from tui_gateway import server_requests as sr


@pytest.fixture(autouse=True)
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    sr.reset_for_tests()
    sr.bind_sinks(lambda frame: None, lambda *args: None, lambda sid: True)
    yield
    sr.reset_for_tests()


def pending(method='approval', params=None, timeout=300):
    values = {'request_id': 'queue-id', 'choices': ['once', 'deny']} if params is None else params
    seen = []
    sr.send_async(method, 'runtime', values, seen.append, timeout=timeout)
    return sr.open_requests('runtime')[0], seen


def body(request, result, action='action-1'):
    return dict(request_id=request['id'], method=request['method'], session_id='runtime',
                request_epoch=request['params']['request_epoch'],
                request_revision=request['params']['request_revision'],
                action_id=action, result=result)


def scope(tmp_path):
    return ('owner', 'connection', 'installation', 'default', 'chat', 'stored', '', 'runtime', str(tmp_path))


def test_deadline_uses_monotonic_and_never_moves(monkeypatch, tmp_path):
    clock = [10.0]
    monkeypatch.setattr(sr.time, 'monotonic', lambda: clock[0])
    request, seen = pending()
    p = request['params']
    assert p['expires_at'] - p['created_at'] == 300
    assert p['timeout_seconds'] == 300
    for elapsed in [250, 299]:
        clock[0] = 10 + elapsed
        assert sr.has_pending_human('runtime')
        assert sr.open_requests('runtime')[0]['params']['expires_at'] == p['expires_at']
    clock[0] = 310
    result = sr.scoped_respond(body(request, {'choice': 'once'}), scope=scope(tmp_path))
    assert result['status'] == 'expired'
    assert seen == [None]
    assert not sr.has_pending_human('runtime')


@pytest.mark.parametrize('result', [{'choice': 'always'}, {'choice': 'session'}, {'choice': 'invalid'},
    {'choice': 'once', 'reason': 'not a denial'}, {'choice': 'deny', 'reason': 'x' * 2001},
    {'choice': 'once', 'all': True}, {'choice': 'once', 'unexpected': True}, {'choice': 1}])
def test_every_transport_validates_before_consuming(result):
    request, seen = pending()
    with pytest.raises((ValueError, PermissionError)):
        sr.resolve_response({'id': request['id'], 'result': result})
    assert seen == []
    assert len(sr.open_requests('runtime')) == 1


def test_scoped_action_race_receipt_and_secret_exclusion(tmp_path):
    request, seen = pending('sudo', {'command': 'dummy'}, timeout=300)
    answer = body(request, {'value': 'DUMMY-SECRET-NOT-A-CREDENTIAL'})
    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(lambda _: sr.scoped_respond(answer, scope=scope(tmp_path)), range(3)))
    assert all(out['status'] == 'accepted' for out in outcomes)
    assert seen == [{'value': 'DUMMY-SECRET-NOT-A-CREDENTIAL'}]
    receipt = sr.action_status(answer, scope=scope(tmp_path))
    assert receipt['status'] == 'accepted'
    assert 'DUMMY-SECRET' not in json.dumps(receipt)
    assert 'DUMMY-SECRET' not in repr(sr._receipts)
    answer['action_id'] = 'different-action'
    assert sr.scoped_respond(answer, scope=scope(tmp_path))['status'] == 'already_resolved'


def test_wrong_epoch_profile_revision_refused_without_consumption(tmp_path):
    request, seen = pending()
    answer = body(request, {'choice': 'once'})
    answer['request_epoch'] = 'old-server'
    assert sr.scoped_respond(answer, scope=scope(tmp_path))['status'] == 'conflict'
    answer = body(request, {'choice': 'once'}, 'action-2')
    foreign = (*scope(tmp_path)[:-1], str(tmp_path / 'other-profile'))
    with pytest.raises(PermissionError):
        sr.scoped_respond(answer, scope=foreign)
    assert not seen
    answer['request_revision'] = 9
    assert sr.scoped_respond(answer, scope=scope(tmp_path))['status'] == 'conflict'
    assert sr.open_requests('runtime')


def test_unlimited_clarify_and_locks_restore(tmp_path):
    request, seen = pending('clarify', {'questions': [
        {'qid': 'q1', 'question': 'One?'}, {'qid': 'q2', 'question': 'Two?'}]}, timeout=None)
    assert request['params']['expires_at'] is None
    answer = body(request, {'answer': 'yes'})
    answer['question_id'] = 'q1'
    assert sr.scoped_respond(answer, scope=scope(tmp_path))['status'] == 'accepted'
    replay = sr.open_requests('runtime')[0]
    assert replay['params']['answers'] == {'q1': 'yes'}
    assert replay['params']['request_revision'] > request['params']['request_revision']
    stale = body(request, {'answer': None}, 'stale-action')
    stale['question_id'] = 'q2'
    assert sr.scoped_respond(stale, scope=scope(tmp_path))['status'] == 'conflict'
    answer = body(replay, {'answer': None}, 'action-2')
    answer['question_id'] = 'q2'
    assert sr.scoped_respond(answer, scope=scope(tmp_path))['status'] == 'accepted'
    assert seen == [{'answers': {'q1': 'yes', 'q2': None}, 'outcome': 'submitted'}]


def test_legacy_answer_is_visible_to_action_reconciliation(tmp_path):
    request, seen = pending()
    assert sr.resolve_response({'id': request['id'], 'result': {'choice': 'deny', 'reason': 'Read only instead'}})
    assert seen == [{'choice': 'deny', 'reason': 'Read only instead'}]
    assert sr.scoped_respond(body(request, {'choice': 'once'}), scope=scope(tmp_path))['status'] == 'already_resolved'


def test_invalid_secret_result_never_echoes(caplog):
    request, seen = pending('vault.code', {'site': 'Example'})
    with pytest.raises(ValueError) as exc:
        sr.resolve_response({'id': request['id'], 'result': {'value': {'secret': 'DUMMY-NO-ECHO'}}})
    assert 'DUMMY-NO-ECHO' not in str(exc.value) + caplog.text
    assert not seen
