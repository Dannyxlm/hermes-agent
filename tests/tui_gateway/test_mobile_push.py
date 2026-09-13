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
