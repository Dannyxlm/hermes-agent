"""Request binding and content-free reconciliation on isolated SQLite stores."""
import pytest


def test_not_now_is_exact_request_bound_and_durable(tmp_path):
    from tools.desktop_spaces import SpaceStore
    store = SpaceStore(tmp_path / 'spaces.sqlite')
    store.publish_signin('studio', 'scope', request_id='req', revision=1,
                         origin='https://example.com', reason='Sign in to continue', expires_at=100)
    assert store.snapshot('studio', 'other', now=10)['signin'] is None
    body = {'action_id': 'act', 'action': 'not_now', 'request_id': 'req', 'request_revision': 1,
            'expected_revision': 2, 'expected_lease_epoch': 0}
    wrong = store.not_now('studio', 'scope', {**body, 'request_revision': 2}, now=10)
    assert wrong['status'] == 'conflict'
    assert store.snapshot('studio', 'scope', now=10)['signin']['id'] == 'req'
    accepted = store.not_now('studio', 'scope', {**body, 'action_id': 'correct'}, now=11)
    assert accepted['status'] == 'accepted'
    restarted = SpaceStore(tmp_path / 'spaces.sqlite')
    assert restarted.action_status('studio', 'scope', 'correct', now=12)['status'] == 'accepted'
    assert restarted.action_status('studio', 'foreign', 'correct', now=12)['status'] == 'unknown'
    assert restarted.not_now('studio', 'scope', {**body, 'action_id': 'correct'}, now=12) == accepted
    assert restarted.not_now('studio', 'scope', {**body, 'action_id': 'correct', 'request_id': 'other'}, now=12)['status'] == 'conflict'
    state = restarted.snapshot('studio', 'scope', now=12)
    assert state['signin'] is None
    assert state['activity'][0]['label'] == 'Sign-in deferred'
    assert state['history'] == [] and 'history' in state['unavailable']


def test_expired_and_replaced_requests_do_not_resolve(tmp_path):
    from tools.desktop_spaces import SpaceStore
    store = SpaceStore(tmp_path / 'spaces.sqlite')
    store.publish_signin('desktop', 'scope', request_id='req', revision=1,
                         origin='https://example.com', reason='Continue', expires_at=10)
    body = {'action_id': 'expired', 'action': 'not_now', 'request_id': 'req', 'request_revision': 1,
            'expected_revision': 2, 'expected_lease_epoch': 0}
    assert store.not_now('desktop', 'scope', body, now=10)['status'] == 'expired'
    store.publish_signin('desktop', 'scope', request_id='replacement', revision=2,
                         origin='https://other.example', reason='Continue', expires_at=100)
    assert store.not_now('desktop', 'scope', {**body, 'action_id': 'stale', 'expected_revision': 3}, now=11)['status'] == 'conflict'
    assert store.snapshot('desktop', 'scope', now=11)['signin']['origin'] == 'https://other.example'


@pytest.mark.parametrize('origin', ['http://example.com', 'https://example.com/path',
    'https://user:password@example.com', 'https://business.facebook.com',
    'https://instagram.com', 'https://127.0.0.1', 'https://messenger.com', 'https://threads.net',
    'https://127.1', 'https://0x7f.0.0.1', 'https://0177.0.0.1', 'https://0X7F.1',
    'https://1.2.3.4.5', 'https://auth.meta.com', 'https://meta.com', 'https://fb.com',
    'https://login.fb.com', 'https://www.whatsapp.com', 'https://whatsapp.com',
    'https://１２７.０.０.１', 'https://ｍｅｔａ.com'])
def test_login_origin_restrictions(tmp_path, origin):
    from tools.desktop_spaces import SpaceStore
    with pytest.raises(ValueError):
        SpaceStore(tmp_path / 'spaces.sqlite').publish_signin('studio', 'scope', request_id='req', revision=1,
            origin=origin, reason='Continue', expires_at=100)


def test_activity_is_bounded_and_scope_filtered(tmp_path):
    from tools.desktop_spaces import SpaceStore
    store = SpaceStore(tmp_path / 'spaces.sqlite')
    for i in range(25):
        store.publish_signin('studio', 'scope', request_id=f'req{i}', revision=1,
                             origin='https://example.com', reason='Continue', expires_at=1000)
        snapshot = store.snapshot('studio', 'scope', now=10)
        receipt = store.not_now('studio', 'scope', {'action_id': f'act{i}', 'action': 'not_now',
            'request_id': f'req{i}', 'request_revision': 1, 'expected_revision': snapshot['revision'],
            'expected_lease_epoch': 0}, now=20+i)
        assert receipt['status'] == 'accepted'
    assert len(store.snapshot('studio', 'scope', now=50)['activity']) == 20
    assert store.snapshot('studio', 'other', now=50)['activity'] == []
