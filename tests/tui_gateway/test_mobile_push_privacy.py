"""Cold clients can narrow delivery without losing destinations they must later restore."""
import json
import uuid

import pytest

from tests.tui_gateway.test_mobile_push import delivery as delivery
from tui_gateway.mobile_push import Scope
from tui_gateway.mobile_push_payloads import AlertPreview
from tui_gateway.mobile_push_provider import DeliveryResult


def subscription(service, sid):
    with service.store._lock:
        return dict(service.store._db.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone())


@pytest.mark.parametrize('surface', ['native', 'native_session'])
def test_tokenless_narrowing_retains_scope_token_and_expiry_then_fresh_token_restores(delivery, surface):
    service, sender, now, ids, _ = delivery
    scope = Scope(surface, 'ops', 'retained-root')
    receipt = service.register('p', scope, **ids, token='aa' * 32, environment='production', preview_enabled=True)
    before = subscription(service, receipt['subscription_id'])
    now[0] += 60
    options = dict(**ids, environment='production', accepts_scope=lambda s: s == scope)
    result = service.refresh('p', **options, token=None, categories=[], preview_enabled=False)
    after = subscription(service, receipt['subscription_id'])
    assert result == {'updated': 1, 'subscriptions': [{**receipt, 'categories': [], 'preview_enabled': False}]}
    assert (after['token'], after['expires_at']) == (before['token'], before['expires_at'])
    assert json.loads(after['categories']) == [] and not after['preview_enabled']
    assert service.refresh('p', **options, token=None, categories=['attention', 'completion'], preview_enabled=True)['updated'] == 1
    still_narrow = subscription(service, receipt['subscription_id'])
    assert json.loads(still_narrow['categories']) == [] and not still_narrow['preview_enabled']
    service.refresh('p', **options, token='bb' * 32, categories=['completion'], preview_enabled=True)
    restored = subscription(service, receipt['subscription_id'])
    assert restored['token'] == 'bb' * 32 and restored['expires_at'] > before['expires_at']
    assert json.loads(restored['categories']) == ['completion'] and restored['preview_enabled']
    run = service.start_run(scope)
    service.record(scope, run['run_id'], 'reply', 'complete', preview=AlertPreview('Fixture', 'Restored reply'))
    service.drain_once()
    assert len(sender.jobs) == 1
    assert sender.jobs[0]['payload']['aps']['alert']['body'] == 'Restored reply'


def test_tokenless_refresh_narrows_queued_payload_and_preserves_allowed_attention(delivery):
    service, sender, _, ids, scope = delivery
    service.register('p', scope, **ids, token='aa' * 32, environment='production', preview_enabled=True)
    run = service.start_run(scope)
    service.record(scope, run['run_id'], 'done', 'complete', preview=AlertPreview('Private title', 'Private reply'))
    service.refresh('p', **ids, token=None, environment='production', categories=['attention'],
                    preview_enabled=False, accepts_scope=lambda _: True)
    service.drain_once()
    assert sender.jobs == []
    next_run = service.start_run(scope)
    service.record(scope, next_run['run_id'], 'question', 'waitingForClarification')
    service.drain_once()
    assert len(sender.jobs) == 1
    assert sender.jobs[0]['payload']['hermex.status'] == 'waitingForClarification'
    assert 'Private' not in json.dumps(sender.jobs[0]['payload'])


def test_tokenless_preview_opt_out_rewrites_already_queued_alert(delivery):
    service, sender, _, ids, scope = delivery
    service.register('p', scope, **ids, token='aa' * 32, environment='production', preview_enabled=True)
    run = service.start_run(scope)
    service.record(scope, run['run_id'], 'done', 'complete', preview=AlertPreview('Private title', 'Private reply'))
    service.refresh('p', **ids, token=None, environment='production', preview_enabled=False,
                    accepts_scope=lambda _: True)
    service.drain_once()
    assert len(sender.jobs) == 1
    assert 'Private' not in json.dumps(sender.jobs[0]['payload'])


def test_tokenless_refresh_keeps_identity_surface_expiry_and_logout_boundaries(delivery):
    service, _, now, ids, scope = delivery
    kept = service.register('p', scope, **ids, token='aa' * 32, environment='production')
    foreign = service.register('other', scope, **ids, token='bb' * 32, environment='production')
    other_device = service.register('p', scope, **{**ids, 'installation_id': str(uuid.uuid4())},
                                    token='cc' * 32, environment='production')
    other_surface = service.register('p', Scope('native_session', 'ops', 'ordinary'), **ids,
                                     token='dd' * 32, environment='production')
    expired = service.register('p', Scope('native', 'ops', 'expired'), **ids, token='ee' * 32,
                               environment='production')
    with service.store.transaction() as db:
        db.execute('UPDATE subscriptions SET expires_at=? WHERE id=?', (now[0] - 1, expired['subscription_id']))
    unchanged = {r['subscription_id']: subscription(service, r['subscription_id'])
                 for r in (foreign, other_device, other_surface, expired)}
    options = dict(**ids, token=None, environment='production', categories=[],
                   accepts_scope=lambda value: value.surface == 'native')
    assert service.refresh('p', **options)['updated'] == 1
    assert all(subscription(service, sid) == row for sid, row in unchanged.items())
    service.unregister('p', **ids, subscription_id=kept['subscription_id'])
    assert service.refresh('p', **options)['updated'] == 0


def test_tokenless_refresh_cannot_resurrect_concurrently_unregistered_scope(delivery):
    service, _, _, ids, scope = delivery
    service.register('p', scope, **ids, token='aa' * 32, environment='production')
    def authorize(_):
        service.unregister('p', **ids)
        return True
    assert service.refresh('p', **ids, token=None, environment='production', categories=[],
                           accepts_scope=authorize)['updated'] == 0


def test_late_invalid_delivery_cannot_delete_tokenless_refreshed_subscription(delivery):
    service, _, _, ids, scope = delivery
    receipt = service.register('p', scope, **ids, token='aa' * 32, environment='production')
    run = service.start_run(scope)
    service.record(scope, run['run_id'], 'attention', 'waitingForApproval')
    claimed = service.store.claim()
    assert claimed is not None
    service.refresh('p', **ids, token=None, environment='production', categories=['attention'],
                    accepts_scope=lambda _: True)
    service.store.finish(claimed, DeliveryResult('invalid'))
    retained = subscription(service, receipt['subscription_id'])
    assert retained['version'] > claimed['token_version']
    assert json.loads(retained['categories']) == ['attention']


@pytest.mark.parametrize('token', ['', 'invalid', 'a' * 33])
def test_invalid_token_does_not_select_privacy_only_refresh(delivery, token):
    service, _, _, ids, _ = delivery
    with pytest.raises(ValueError, match='invalid token'):
        service.refresh('p', **ids, token=token, environment='production', accepts_scope=lambda _: True)


def test_missing_token_is_not_allowed_for_registration_or_activity_refresh(delivery):
    service, _, _, ids, scope = delivery
    with pytest.raises(ValueError, match='invalid token'):
        service.register('p', scope, **ids, token=None, environment='production')
    with pytest.raises(ValueError, match='invalid token'):
        service.refresh('p', **ids, token=None, environment='production', kind='activity',
                        activity_id='fixture', run_id='fixture', accepts_scope=lambda _: True)
