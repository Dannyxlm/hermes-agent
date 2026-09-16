"""Durable delivery uses real SQLite, scoped identities and a fake Apple endpoint only."""

import json
import uuid

import pytest

from tui_gateway.mobile_push import PushService, Scope
from tui_gateway.mobile_push_provider import DeliveryResult


class Sender:
    def __init__(self):
        self.jobs = []
        self.result = DeliveryResult("accepted")

    def send(self, job):
        self.jobs.append(job)
        return self.result

    def close(self):
        pass


@pytest.mark.parametrize("surface", ["native", "chats"])
@pytest.mark.parametrize("status", ["complete", "waitingForApproval", "waitingForClarification"])
def test_alert_projection_reaches_notification_extension_without_private_content(tmp_path, surface, status):
    now = [1800000000.0]
    sender = Sender()
    service = PushService(tmp_path / "attention.sqlite", sender, clock=lambda: now[0])
    scope = Scope(surface, "fixture-profile", "fixture-session")
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    try:
        service.register("fixture-principal", scope, **ids, token="aa" * 32, environment="production")
        run = service.start_run(scope)
        now[0] += 5
        service.record(scope, run["run_id"], "fixture-event", status)
        service.drain_once()
        assert len(sender.jobs) == 1
        payload = sender.jobs[0]["payload"]
        assert payload["aps"]["mutable-content"] == 1
        assert payload["hermex.status"] == status
        assert payload["hermex.run_started_at"] == run["started_at"]
        assert payload["hermex.updated_at"] == now[0]
        assert payload["hermex.destination"]["surface"] == surface
        assert payload["hermex.destination"]["run_id"] == run["run_id"]
        assert "fixture-session" not in json.dumps(payload["aps"])
        assert "fixture-profile" not in json.dumps(payload["aps"])
        assert "aa" * 32 not in json.dumps(payload)
    finally:
        service.close()


def test_registered_run_completes_once_across_restart(tmp_path):
    now = [1800000000.0]
    sender = Sender()
    scope = Scope("native", "ops", "root")
    path = tmp_path / "push.sqlite"
    service = PushService(path, sender, clock=lambda: now[0])
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    service.register("principal", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.close()
    service = PushService(path, sender, clock=lambda: now[0])
    assert service.current_run(scope)["run_id"] == run["run_id"]
    service.record(scope, run["run_id"], "terminal", "complete")
    service.record(scope, run["run_id"], "duplicate-terminal", "complete")
    service.drain_once()
    assert len(sender.jobs) == 1
    dest = sender.jobs[0]["payload"]["hermex.destination"]
    assert dest == {"version": 1, "surface": "native", **ids, "profile": "ops",
                    "canonical_root_id": "root", "run_id": run["run_id"]}
    assert service.unregister("other-principal", **ids) == 0
    assert service.unregister("principal", **ids) == 1
    service.close()


@pytest.fixture
def delivery(tmp_path):
    now = [1800000000.0]
    sender = Sender()
    service = PushService(tmp_path / "push.sqlite", sender, clock=lambda: now[0])
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    yield service, sender, now, ids, Scope("native", "ops", "root")
    service.close()


def test_token_rotation_invalid_old_token_does_not_delete_new_registration(delivery):
    service, sender, now, ids, scope = delivery
    receipt = service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "attention", "waitingForApproval")
    job = service.store.claim()
    rotated = service.register("p", scope, **ids, token="bb" * 32, environment="production")
    assert rotated["subscription_id"] == receipt["subscription_id"]
    service.store.finish(job, DeliveryResult("invalid"))
    service.record(scope, run["run_id"], "complete", "complete")
    service.drain_once()
    assert [j["token"] for j in sender.jobs] == ["bb" * 32]
    sender.result = DeliveryResult("invalid")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "complete", "complete")
    service.drain_once()
    assert service.unregister("p", **ids) == 0


def test_retry_lease_survives_restart_and_is_bounded(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "complete", "complete")
    first = service.store.claim()
    from tui_gateway.mobile_push_store import PushStore
    other = PushStore(service.store.path, lambda: now[0])
    assert other.claim() is None
    now[0] += 31
    recovered = other.claim()
    assert recovered["job_id"] == first["job_id"]
    other.finish(recovered, DeliveryResult("retry"))
    assert other.claim() is None
    other.close()
    sender.result = DeliveryResult("retry")
    for _ in range(9):
        now[0] += 901
        service.drain_once()
    assert len(sender.jobs) <= 6  # eight total attempts including the two lease claims
    assert service.store.claim() is None


def test_activity_is_permission_independent_scoped_coalesced_and_ended(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    receipt = service.register("p", scope, **ids, token="cc" * 32, environment="production",
        kind="activity", activity_id="apple-activity", run_id=run["run_id"])
    with pytest.raises(ValueError, match="run unavailable"):
        service.register("p", Scope("native", "other", "other-root"), **ids, token="cc" * 32,
            environment="production", kind="activity", activity_id="wrong", run_id=run["run_id"])
    for step, status in enumerate(["thinking", "usingTool", "responding"]):
        now[0] += 1
        service.record(scope, run["run_id"], str(step), status)
    assert service.drain_once() == 0
    now[0] += 10
    assert service.drain_once() == 1
    payload = sender.jobs[-1]["payload"]["aps"]
    assert payload["event"] == "update"
    assert payload["content-state"]["status"] == "responding"
    assert payload["content-state"]["startedAt"] == run["started_at"] - 978307200
    assert payload["timestamp"] > 978307200
    service.record(scope, run["run_id"], "done", "complete")
    now[0] += 1
    service.drain_once()
    assert sender.jobs[-1]["payload"]["aps"]["event"] == "end"
    service.register("p", scope, **ids, token="dd" * 32, environment="production",
        kind="activity", activity_id="apple-activity", run_id=run["run_id"])
    now[0] += 1
    service.drain_once()
    assert sender.jobs[-1]["token"] == "dd" * 32
    assert sender.jobs[-1]["payload"]["aps"]["event"] == "end"
    assert service.unregister("p", **ids, subscription_id=receipt["subscription_id"], kind="activity") == 1


def test_unsubscribe_removes_pending_jobs_and_categories_control_alerts(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production", categories=["attention"])
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "attention", "waitingForApproval")
    service.record(scope, run["run_id"], "attention", "waitingForApproval")
    service.drain_once()
    assert len(sender.jobs) == 1
    service.record(scope, run["run_id"], "done", "complete")
    assert service.drain_once() == 0
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "attention", "waitingForApproval")
    assert service.unregister("p", **ids) == 1
    assert service.drain_once() == 0
    assert len(sender.jobs) == 1


@pytest.mark.parametrize("old_delivery", ["queued", "retry", "inflight"])
def test_activity_resumption_supersedes_old_attention_even_across_retry(delivery, old_delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    service.register("p", scope, **ids, token="cc" * 32, environment="production",
                     kind="activity", activity_id="activity", run_id=run["run_id"])
    service.record(scope, run["run_id"], "approval", "waitingForApproval")
    old = None if old_delivery == "queued" else service.store.claim()
    if old_delivery == "retry":
        service.store.finish(old, DeliveryResult("retry"))
    now[0] += 1
    service.record(scope, run["run_id"], "resumed", "responding")
    if old_delivery == "inflight":
        service.store.finish(old, DeliveryResult("retry"))
    for delay in (12, 60):
        now[0] += delay
        service.drain_once()
    assert [job["payload"]["aps"]["content-state"]["status"] for job in sender.jobs] == ["responding"]


def test_expired_activity_ends_before_token_expiry_and_devices_expire(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.register("p", scope, **ids, token="cc" * 32, environment="production", kind="activity",
                     activity_id="activity", run_id=run["run_id"])
    now[0] += 8 * 3600 - 100
    service.drain_once()
    assert any(j["kind"] == "activity" and j["payload"]["aps"]["event"] == "end" for j in sender.jobs)
    assert not any(j["kind"] == "alert" for j in sender.jobs)
    assert service.current_run(scope)["status"] == "starting"
    service.record(scope, run["run_id"], "actual-completion", "complete")
    service.drain_once()
    assert any(j["kind"] == "alert" for j in sender.jobs)
    now[0] += 31 * 86400
    service.store.maintain()
    assert service.unregister("p", **ids) == 0


def test_rotation_retries_inflight_completion_and_opt_out_gates_queued_alerts(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "done", "complete")
    old = service.store.claim()
    service.register("p", scope, **ids, token="bb" * 32, environment="production")
    service.store.finish(old, DeliveryResult("invalid"))
    now[0] += 11
    service.drain_once()
    assert sender.jobs[-1]["token"] == "bb" * 32
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "done", "complete")
    service.register("p", scope, **ids, token="bb" * 32, environment="production", categories=[])
    assert service.drain_once() == 0


def test_disabled_provider_still_allows_owned_logout_cleanup(tmp_path, monkeypatch):
    from tui_gateway import mobile_push
    home = tmp_path / "home"
    path = home / "mobile-push" / "outbox.sqlite3"
    service = PushService(path, Sender())
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    service.register("p", Scope("chats", "ops", "parent/child"), **ids, token="aa" * 32, environment="production")
    service.close()
    monkeypatch.setitem(mobile_push._services, home.resolve(), None)
    assert mobile_push.unregister_for_home(home, "foreign", **ids) == 0
    assert mobile_push.unregister_for_home(home, "p", **ids) == 1


def test_provider_uses_fixed_topic_and_privacy_safe_error_classification(tmp_path):
    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from tui_gateway.mobile_push_provider import APNsProvider
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "test.p8"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    path.chmod(0o600)
    requests = []
    statuses = [(200, {}), (503, {}), (410, {"reason": "Unregistered"}), (403, {"reason": "InvalidProviderToken"})]
    def handler(request):
        requests.append(request)
        status, payload = statuses.pop(0)
        return httpx.Response(status, json=payload)
    provider = APNsProvider({"team_id": "8AA929B9Q5", "key_id": "TESTKEY123", "private_key_path": str(path)},
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    job = {"kind": "activity", "environment": "production", "job_id": str(uuid.uuid4()),
           "expires_at": 1800000200, "urgent": True, "collapse_id": "test", "token": "aa" * 32,
           "payload": {"aps": {"event": "end"}}, "topic": "attacker.topic"}
    assert [provider.send(job).outcome for _ in range(4)] == ["accepted", "retry", "invalid", "failed"]
    assert requests[0].headers["apns-topic"] == "co.cloudseed.hermex.ava.push-type.liveactivity"
    assert requests[0].url.host == "api.push.apple.com"
    assert requests[0].headers["apns-push-type"] == "liveactivity"
    assert json.loads(requests[0].content) == job["payload"]
    statuses.append((200, {}))
    widget = {**job, "kind": "widget", "urgent": False, "payload": {"aps": {"content-changed": True}}}
    assert provider.send(widget).outcome == "accepted"
    assert requests[-1].headers["apns-topic"] == "co.cloudseed.hermex.ava.push-type.widgets"
    assert requests[-1].headers["apns-push-type"] == "widgets"
    assert requests[-1].headers["apns-priority"] == "5"
    assert json.loads(requests[-1].content) == widget["payload"]
    provider.close()


def test_optional_storage_failure_does_not_break_startup(tmp_path, monkeypatch):
    from tui_gateway import mobile_push
    home = tmp_path / "broken-home"
    home.mkdir()
    (home / "config.yaml").write_text("mobile_push:\n  enabled: true\n")
    state = home / "mobile-push"
    state.mkdir()
    (state / "outbox.sqlite3").write_bytes(b"not a SQLite database")
    closed = []
    class Provider:
        def __init__(self, config):
            pass
        def close(self):
            closed.append(True)
    monkeypatch.setattr(mobile_push, "APNsProvider", Provider)
    assert mobile_push.service_for_home(home) is None
    assert closed == [True]


def test_activity_timestamp_follows_dispatch_clock_not_delta_count(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    service.register("p", scope, **ids, token="cc" * 32, environment="production",
                     kind="activity", activity_id="activity", run_id=run["run_id"])
    for step in range(500):
        service.record(scope, run["run_id"], str(step), "thinking" if step % 2 else "usingTool")
    now[0] += 10
    assert service.drain_once() == 1
    first = sender.jobs[-1]["payload"]["aps"]["timestamp"]
    assert first == int(now[0])
    service.record(scope, run["run_id"], "done", "complete")
    assert service.drain_once() == 0
    now[0] += 1
    assert service.drain_once() == 1
    assert sender.jobs[-1]["payload"]["aps"]["timestamp"] == first + 1
    assert sender.jobs[-1]["payload"]["aps"]["content-state"]["updatedAt"] <= now[0] - 978307200


def test_alert_event_identity_survives_restart_and_retry(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "private-request-id", "waitingForApproval")
    sender.result = DeliveryResult("retry")
    service.drain_once()
    first = sender.jobs[-1]["payload"]["event_id"]
    assert len(first) == 64 and "private-request-id" not in first
    from tui_gateway.mobile_push_store import PushStore
    restarted = PushStore(service.store.path, lambda: now[0])
    now[0] += 11
    retried = restarted.claim()
    assert retried["payload"]["event_id"] == first
    restarted.close()


def test_detached_refresh_rotates_only_live_owned_subscriptions(delivery):
    service, sender, now, ids, scope = delivery
    scopes = [scope, Scope("native", "other", "other-root")]
    for item in scopes:
        service.register("p", item, **ids, token="aa" * 32, environment="production")
    expired = service.register("p", Scope("native", "expired", "old-root"), **ids,
                               token="aa" * 32, environment="production")
    with service.store.transaction() as db:
        db.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (now[0] - 1, expired["subscription_id"]))
    service.register("foreign", scope, **ids, token="cc" * 32, environment="production")
    run = service.start_run(scope)
    activity = service.register("p", scope, **ids, token="dd" * 32, environment="production",
                               kind="activity", activity_id="live", run_id=run["run_id"])
    service.record(scope, run["run_id"], "attention", "waitingForApproval")
    old = service.store.claim()
    refreshed = service.refresh("p", **ids, token="bb" * 32, environment="production",
                                categories=[], accepts_scope=lambda value: value.surface == "native")
    assert refreshed["updated"] == 2
    service.store.finish(old, DeliveryResult("invalid"))
    with service.store._lock:
        rows = service.store._db.execute("SELECT * FROM subscriptions").fetchall()
    assert len(rows) == 5
    assert all(row["token"] == "bb" * 32 for row in rows if row["principal"] == "p" and row["profile"] in {"ops", "other"} and row["kind"] == "alert")
    assert next(row for row in rows if row["id"] == activity["subscription_id"])["token"] == "dd" * 32
    assert next(row for row in rows if row["principal"] == "foreign")["token"] == "cc" * 32
    assert service.refresh("missing", **ids, token="bb" * 32, environment="production",
                           categories=[], accepts_scope=lambda value: True)["updated"] == 0
    assert service.refresh("p", **{**ids, "connection_id": str(uuid.uuid4())}, token="bb" * 32,
                           environment="production", categories=[], accepts_scope=lambda value: True)["updated"] == 0
    service.record(scope, run["run_id"], "complete", "complete")
    service.drain_once()
    assert not any(job["kind"] == "alert" and job["token"] == "bb" * 32 for job in sender.jobs)


def test_detached_activity_refresh_never_recreates_and_replays_terminal(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    service.register("p", scope, **ids, token="aa" * 32, environment="production", categories=[])
    receipt = service.register("p", scope, **ids, token="cc" * 32, environment="production",
                               kind="activity", activity_id="live", run_id=run["run_id"])
    service.record(scope, run["run_id"], "complete", "complete")
    service.drain_once()
    options = dict(**ids, token="dd" * 32, environment="production", kind="activity",
                   activity_id="live", run_id=run["run_id"], accepts_scope=lambda value: value == scope)
    assert service.refresh("p", **options)["updated"] == 1
    now[0] += 1
    service.drain_once()
    assert sender.jobs[-1]["token"] == "dd" * 32
    assert sender.jobs[-1]["payload"]["aps"]["event"] == "end"
    assert service.refresh("p", **{**options, "activity_id": "forged"})["updated"] == 0
    assert service.refresh("p", **{**options, "accepts_scope": lambda value: False})["updated"] == 0
    service.unregister("p", **ids, subscription_id=receipt["subscription_id"])
    assert service.refresh("p", **options)["updated"] == 0


@pytest.mark.parametrize("surface", ["native", "chats"])
def test_reply_previews_are_per_device_and_survive_delivery_restart(delivery, surface):
    from tui_gateway.mobile_push_payloads import AlertPreview
    service, sender, now, ids, _ = delivery
    scope = Scope(surface, "ops", "preview-session")
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    preview_ids = {**ids, "installation_id": str(uuid.uuid4())}
    service.register("p", scope, **preview_ids, token="bb" * 32, environment="production", preview_enabled=True)
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "done", "complete", preview=AlertPreview(
        title="Weekly **briefing**", reply="# Ready\n\nThe [brief](https://example.org/private) is **ready**.\n```private tool output```"))
    from tui_gateway.mobile_push_store import PushStore
    restarted = PushStore(service.store.path, lambda: now[0])
    try:
        jobs = [restarted.claim(), restarted.claim()]
        alerts = {j["token"]: j["payload"]["aps"]["alert"] for j in jobs}
        assert alerts["aa" * 32]["title"] == "Hermex Ava"
        assert alerts["bb" * 32] == {"title": "Weekly briefing", "body": "Ready The brief is ready."}
        assert all(j["payload"]["aps"]["mutable-content"] == 1 for j in jobs)
    finally:
        restarted.close()


def test_preview_opt_out_controls_already_queued_notification(delivery):
    from tui_gateway.mobile_push_payloads import AlertPreview
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production", preview_enabled=True)
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "done", "complete", preview=AlertPreview("Private title", "Private reply"))
    service.refresh("p", **ids, token="aa" * 32, environment="production", accepts_scope=lambda _: True, preview_enabled=False)
    service.drain_once()
    alert = sender.jobs[0]["payload"]["aps"]["alert"]
    assert "Private" not in json.dumps(alert)
    assert alert["title"] == "Hermex Ava"


def test_preview_migration_keeps_old_subscriptions_generic(delivery):
    from tui_gateway.mobile_push_store import PushStore
    service, sender, now, ids, scope = delivery
    receipt = service.register("p", scope, **ids, token="aa" * 32, environment="production")
    with service.store.transaction() as db:
        db.execute("ALTER TABLE subscriptions DROP COLUMN preview_enabled")
    migrated = PushStore(service.store.path, lambda: now[0])
    try:
        with migrated.transaction() as db:
            row = db.execute("SELECT id,preview_enabled FROM subscriptions").fetchone()
        assert row["id"] == receipt["subscription_id"]
        assert row["preview_enabled"] == 0
    finally:
        migrated.close()


@pytest.mark.parametrize("bad", ["false", "true", 1, None, {}])
def test_preview_requires_boolean_preference(delivery, bad):
    service, _, _, ids, scope = delivery
    with pytest.raises(ValueError, match="boolean"):
        service.register("p", scope, **ids, token="aa" * 32, environment="production", preview_enabled=bad)


def test_preview_bounds_unicode_and_never_uses_error_as_reply():
    from tui_gateway.mobile_push_payloads import AlertPreview
    preview = AlertPreview("A\u202e" + "🦋" * 100, "**Reply** " + "🦋" * 300)
    alert = preview.alert("complete")
    assert len(alert["title"]) <= 80 and len(alert["body"]) <= 240
    assert "\u202e" not in alert["title"]
    assert "🦋" not in preview.alert("failed")["body"]
    assert AlertPreview({}, {}).alert("complete")["title"] == "Hermex Ava"


def test_widget_reads_are_scoped_monotonic_revocable_and_credential_free(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("chats", "ops", "chat")
    cap = "12" * 32
    service.register("owner", scope, **ids, token="", environment="production", kind="widget", read_token=cap)
    other = Scope("chats", "other", "hidden")
    service.start_run(other)
    first = service.start_run(scope)
    service.record(scope, first['run_id'], "done", "complete")
    snapshot = service.store.widget_snapshot(cap)
    assert len(snapshot['items']) == 1 and snapshot['items'][0]['run_id'] == first['run_id']
    assert cap not in json.dumps(snapshot)
    # Same timestamp: latest inserted run still wins.
    second = service.start_run(scope)
    assert service.store.widget_snapshot(cap)['items'][0]['run_id'] == second['run_id']
    service.drain_once()
    assert sender.jobs == []  # no push token yet
    with pytest.raises(PermissionError):
        service.store.widget_snapshot("34" * 32)
    with pytest.raises(ValueError):
        service.register("other-owner", scope, **ids, token="", environment="production", kind="widget", read_token=cap)
    assert service.unregister("other-owner", **ids) == 0
    assert service.store.widget_snapshot(cap)['items']
    service.unregister("owner", **ids)
    with pytest.raises(PermissionError):
        service.store.widget_snapshot(cap)


def test_widget_push_coalesces_persisted_changes_and_rotation_preserves_other_alerts(delivery):
    service, sender, now, ids, _ = delivery
    cap = "12" * 32
    scopes = [Scope("chats", "ops", sid) for sid in ("one", "two")]
    for scope in scopes:
        service.register("owner", scope, **ids, token="aa" * 32, environment="production", kind="widget", read_token=cap)
    service.register("owner", scopes[0], **ids, token="bb" * 32, environment="production")
    for scope in scopes:
        run = service.start_run(scope)
        service.record(scope, run['run_id'], "done", "complete")
    service.store.update_widget_token(cap, "cc" * 32)
    now[0] += 3
    service.drain_once()
    widget = [job for job in sender.jobs if job['kind'] == "widget"]
    alerts = [job for job in sender.jobs if job['kind'] == "alert"]
    assert len(widget) == 1 and widget[0]['token'] == "cc" * 32
    assert widget[0]['payload'] == {"aps": {"content-changed": True}}
    assert len(alerts) == 1 and alerts[0]['token'] == "bb" * 32
    assert len(service.store.widget_snapshot(cap)['items']) == 2
    next_run = service.start_run(scopes[0])
    for index, status in enumerate(["thinking", "usingTool", "thinking", "responding"]):
        service.record(scopes[0], next_run['run_id'], str(index), status)
        now[0] += 3
        service.drain_once()
    assert len([job for job in sender.jobs if job['kind'] == 'widget']) == 2
    service.record(scopes[0], next_run['run_id'], "question-before-removal", "waitingForApproval")
    now[0] += 3
    job = service.store.claim()
    while job and job['kind'] != 'widget':
        service.store.finish(job, DeliveryResult("accepted"))
        job = service.store.claim()
    assert job and job['kind'] == 'widget'
    service.store.finish(job, DeliveryResult("invalid"))
    assert len(service.store.widget_snapshot(cap)['items']) == 2
    service.store.update_widget_token(cap, "")
    service.record(scopes[0], next_run['run_id'], "question", "waitingForClarification")
    now[0] += 3
    service.drain_once()
    assert len([job for job in sender.jobs if job['kind'] == 'widget']) == 2
    now[0] += 31 * 86400
    with pytest.raises(PermissionError):
        service.store.widget_snapshot(cap)


@pytest.mark.parametrize("old_delivery", ["queued", "retry", "inflight"])
def test_cancelled_attention_does_not_deliver_stale_alert(delivery, old_delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "question", "waitingForClarification")
    old = None if old_delivery == "queued" else service.store.claim()
    if old_delivery == "retry":
        service.store.finish(old, DeliveryResult("retry"))
    service.record(scope, run["run_id"], "request.cancel", "thinking")
    if old_delivery == "inflight":
        service.store.finish(old, DeliveryResult("retry"))
    now[0] += 120
    assert service.drain_once() == 0
    assert sender.jobs == []
    service.record(scope, run["run_id"], "new-question", "waitingForApproval")
    assert service.drain_once() == 1
    assert sender.jobs[0]["payload"]["hermex.status"] == "waitingForApproval"
