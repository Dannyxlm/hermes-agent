"""ActivityKit completion contracts through the real store/worker, with no Apple traffic."""
import hashlib
import json
import uuid

import pytest

from tests.tui_gateway.test_mobile_push import Sender, delivery as delivery  # noqa: PLC0414
from tui_gateway.mobile_push import PushService, Scope
from tui_gateway.mobile_push_payloads import AlertPreview, activity_payload
from tui_gateway.mobile_push_provider import DeliveryResult


def register_activity(service, scope, ids, run_id, name="live"):
    return service.register("p", scope, **ids, token="cc" * 32, environment="production",
                            kind="activity", activity_id=name, run_id=run_id)


def activities(sender):
    return [j for j in sender.jobs if j["kind"] == "activity"]


@pytest.mark.parametrize("status,extra", [("complete", {}), ("failed", {}),
    ("cancelled", {"closed_reason": "orphaned"}), ("cancelled", {"activity_expired": True})])
def test_finished_card_deadline_is_finish_not_dispatch(status, extra):
    scope = Scope("native_session", "default", "chat")
    run = {"run_id": "run", "status": status, "started_at": 1800000000,
           "updated_at": 1800000030, **extra}
    payload = activity_payload(run, scope, 1800000900)
    assert payload["aps"]["dismissal-date"] == 1800000150


def test_done_lights_island_once_then_ends_eight_seconds_later(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production", preview_enabled=True)
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    now[0] += 1572
    service.record(scope, run["run_id"], "done", "complete",
                   preview=AlertPreview(title="Private title", reply="Private reply", plan_done=3, plan_total=3))
    finished = now[0]
    service.drain_once()
    first = activities(sender)[0]["payload"]["aps"]
    assert first["event"] == "update"
    assert first["content-state"]["isFinal"] is True
    assert first["alert"] == {"title": "Ava · Done", "body": "26m 12s · 3 steps", "sound": "default"}
    assert first["content-state"]["responseExcerpt"] == "Private reply"
    completion = next(j for j in sender.jobs if j["kind"] == "alert")["payload"]["aps"]
    assert completion["interruption-level"] == "passive"
    assert "sound" not in completion
    now[0] += 7
    service.drain_once()
    assert len(activities(sender)) == 1
    now[0] += 1
    service.drain_once()
    end = activities(sender)[-1]["payload"]["aps"]
    assert end["event"] == "end" and "alert" not in end
    assert end["dismissal-date"] == finished + 120
    assert end["content-state"]["sessionTitle"] == "Private title"
    assert end["content-state"]["responseExcerpt"] == "Private reply"
    assert end["timestamp"] > first["timestamp"]


@pytest.mark.parametrize("status", ["complete", "failed", "cancelled"])
def test_terminal_survives_worker_restart_beyond_job_and_lease_expiry(tmp_path, status):
    now = [1800000000.0]
    path = tmp_path / "outbox.sqlite"
    sender = Sender()
    scope = Scope("native_session", "default", "chat")
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    service = PushService(path, sender, clock=lambda: now[0])
    run = service.start_run(scope)
    for name in ("one", "two"):
        register_activity(service, scope, ids, run["run_id"], name)
    now[0] += 15
    service.record(scope, run["run_id"], "terminal", status)
    finished = now[0]
    service.close()
    now[0] += 8 * 3600 + 60  # worker missed both one-hour outbox and eight-hour lease
    service = PushService(path, sender, clock=lambda: now[0])
    try:
        service.drain_once()
        ends = [j for j in activities(sender) if j["payload"]["aps"]["event"] == "end"]
        assert len(ends) == 2
        assert all(j["payload"]["aps"]["dismissal-date"] == finished + 120 for j in ends)
        assert all("alert" not in j["payload"]["aps"] for j in ends)
        service.drain_once()
        assert len(activities(sender)) == 2
    finally:
        service.close()


def test_missed_lifetime_window_still_ends_projection_without_ending_run(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    now[0] += 8 * 3600 + 600
    service.drain_once()
    jobs = activities(sender)
    assert len(jobs) == 1 and jobs[0]["payload"]["aps"]["event"] == "end"
    content = jobs[0]["payload"]["aps"]["content-state"]
    assert content["activityExpired"] is True
    assert service.current_run(scope)["status"] == "starting"
    assert not [j for j in sender.jobs if j["kind"] == "alert"]


def test_terminal_recorded_after_lease_expiry_still_owns_end(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    now[0] += 8 * 3600 + 20
    service.record(scope, run["run_id"], "done", "failed")
    service.drain_once()
    jobs = activities(sender)
    assert len(jobs) == 1
    assert jobs[0]["payload"]["aps"].get("event") == "end"
    assert jobs[0]["payload"]["aps"]["content-state"].get("status") == "failed"


def test_uncertain_done_send_does_not_repeat_visible_alert_and_end_keeps_retrying(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    now[0] += 2
    service.record(scope, run["run_id"], "done", "complete")
    sender.result = DeliveryResult("retry", "transport")
    for _ in range(12):
        service.drain_once()
        now[0] += 901
    assert sum("alert" in j["payload"]["aps"] for j in activities(sender)) == 1
    sender.result = DeliveryResult("accepted")
    service.drain_once()
    assert activities(sender)[-1]["payload"]["aps"]["event"] == "end"


def test_one_sound_is_scoped_to_device_and_run_even_with_duplicate_activities(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    other = {**ids, "installation_id": str(uuid.uuid4())}
    service.register("p", scope, **other, token="bb" * 32, environment="production")
    run = service.start_run(scope)
    for name in ("one", "two"):
        register_activity(service, scope, ids, run["run_id"], name)
    now[0] += 5
    service.record(scope, run["run_id"], "done", "complete")
    service.drain_once()
    alerts = {j["token"]: j["payload"]["aps"] for j in sender.jobs if j["kind"] == "alert"}
    assert alerts.get("aa" * 32, {}).get("interruption-level") == "passive"
    assert alerts["bb" * 32]["sound"] == "default"
    assert "interruption-level" not in alerts["bb" * 32]
    assert sum("alert" in j["payload"]["aps"] for j in activities(sender)) == 1
    now[0] += 8
    service.drain_once()
    assert sum(j["payload"]["aps"]["event"] == "end" for j in activities(sender)) == 2


@pytest.mark.parametrize("seconds,status,subtitle", [(1500, "complete", None),
    (1501, "complete", "Done · 25m 01s · 3 steps"), (1572, "complete", "Done · 26m 12s · 3 steps"),
    (1572, "failed", None)])
@pytest.mark.parametrize("previews", [False, True])
def test_long_successful_run_subtitle_survives_privacy_at_dispatch(delivery, seconds, status, subtitle, previews):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production", preview_enabled=previews)
    run = service.start_run(scope)
    now[0] += seconds
    service.record(scope, run["run_id"], "done", status,
                   preview=AlertPreview(title="PRIVATE", reply="PRIVATE", plan_total=3))
    if previews:
        service.refresh("p", **ids, token=None, environment="production", preview_enabled=False,
                        accepts_scope=lambda _: True)
    service.drain_once()
    aps = sender.jobs[-1]["payload"]["aps"]
    assert aps["alert"].get("subtitle") == subtitle
    assert "PRIVATE" not in json.dumps(aps)


def test_long_step_heartbeat_refreshes_stale_date_without_changing_content(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    service.record(scope, run["run_id"], "tool", "usingTool", preview=AlertPreview(step="Long command"))
    now[0] += 10
    service.drain_once()
    first = activities(sender)[-1]["payload"]["aps"]["content-state"]
    now[0] += 109
    service.drain_once()
    assert len(activities(sender)) == 1
    now[0] += 2
    service.drain_once()
    assert len(activities(sender)) == 2
    heartbeat = activities(sender)[-1]
    assert heartbeat["urgent"] == 0
    assert heartbeat["payload"]["aps"]["content-state"] == first
    assert heartbeat["payload"]["aps"]["stale-date"] == int(now[0] + 120)
    now[0] += 99
    service.drain_once()
    assert len(activities(sender)) == 2
    now[0] += 1
    service.drain_once()
    assert len(activities(sender)) == 3
    service.record(scope, run["run_id"], "done", "failed")
    service.drain_once()
    now[0] += 200
    service.drain_once()
    assert len(activities(sender)) == 4


@pytest.mark.parametrize("status", ["starting", "thinking", "responding", "usingTool"])
def test_quiet_running_phase_heartbeat_keeps_activity_fresh(delivery, status):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    service.live_runs = lambda: ({run["run_id"]}, {run["run_id"]})
    register_activity(service, scope, ids, run["run_id"])
    service.record(scope, run["run_id"], "phase", status)
    now[0] += 10
    service.drain_once()
    first = activities(sender)[-1]["payload"]["aps"]
    now[0] += 109
    service.drain_once()
    assert len(activities(sender)) == 1
    now[0] += 2
    assert service.record(scope, run["run_id"], "same-phase", status) is False
    service.drain_once()
    assert len(activities(sender)) == 2
    heartbeat = activities(sender)[-1]
    assert heartbeat["urgent"] == 0  # APNs priority 5
    assert heartbeat["payload"]["aps"]["event"] == "update"
    assert heartbeat["payload"]["aps"]["content-state"] == first["content-state"]
    assert heartbeat["payload"]["aps"]["stale-date"] == int(now[0] + 120)
    now[0] += 99
    service.drain_once()
    assert len(activities(sender)) == 2
    now[0] += 1
    service.drain_once()
    assert len(activities(sender)) == 3


@pytest.mark.parametrize("fence", ["terminal", "revoked", "expired", "changed_owner", "orphan", "attention_deadline"])
def test_quiet_phase_heartbeat_respects_lifecycle_fences(delivery, fence):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    service.live_runs = lambda: ({run["run_id"]}, {run["run_id"]})
    receipt = register_activity(service, scope, ids, run["run_id"])
    service.record(scope, run["run_id"], "tool", "usingTool")
    now[0] += 10
    service.drain_once()
    assert len(activities(sender)) == 1
    if fence == "terminal":
        service.record(scope, run["run_id"], "done", "failed")
    elif fence == "revoked":
        service.unregister("p", **ids, subscription_id=receipt["subscription_id"])
    elif fence == "expired":
        with service.store.transaction() as db:
            db.execute("UPDATE subscriptions SET expires_at=? WHERE id=?", (now[0] + 50, receipt["subscription_id"]))
    elif fence == "changed_owner":
        # A different live owner may keep its run open, but this worker must not refresh it.
        service.store.owner = "replacement-owner"
    elif fence == "orphan":
        service.live_runs = lambda: (set(), set())
    else:
        service.record(scope, run["run_id"], "ask", "waitingForApproval",
                       preview=AlertPreview(attention={"nearest_expires_at": now[0] + 30}))
        now[0] += 1
        service.drain_once()
        assert activities(sender)[-1]["payload"]["aps"]["stale-date"] == int(now[0] + 29)
    now[0] += 121
    service.drain_once()
    assert not [j for j in activities(sender)[1:]
                if not j["payload"]["aps"]["content-state"]["isFinal"]
                and j["payload"]["aps"]["content-state"]["status"] == "usingTool"]
    sent = len(activities(sender))
    now[0] += 121
    service.drain_once()
    assert len(activities(sender)) == sent


@pytest.mark.parametrize("outcome", ["accepted", "retry", "invalid", "failed"])
def test_shape_audit_is_content_free_and_correlates_each_attempt(delivery, outcome):
    service, sender, _, ids, scope = delivery
    sender.result = DeliveryResult(outcome)
    service.register("p", scope, **ids, token="aa" * 32, environment="production", preview_enabled=True)
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "PRIVATE_EVENT", "complete",
                   preview=AlertPreview(title="PRIVATE_TITLE", reply="PRIVATE_BODY"))
    service.drain_once()
    audit = service.store.path.parent / "push-shape-audit.jsonl"
    assert audit.is_file()
    text = audit.read_text()
    records = [json.loads(line) for line in text.splitlines()]
    assert len(records) == 1
    assert records[0] == {"kind": "alert", "status": "complete", "title_length": 13, "body_length": 12,
                          "event_id": hashlib.sha256(f"{scope.key}:{run['run_id']}:PRIVATE_EVENT".encode()).hexdigest(),
                          "outcome": outcome, "sent_at": service._clock()}
    assert all(value not in text for value in ("PRIVATE_TITLE", "PRIVATE_BODY", "PRIVATE_EVENT", "aa" * 32))


def test_registered_push_started_activity_gets_silent_late_end(delivery):
    service, sender, now, ids, scope = delivery
    service.store.set_origin(scope, "p", **ids)
    service.register_start_token("p", **ids, token="ee" * 32, environment="production")
    run = service.start_run(scope, continuation=True)
    service.drain_once()
    assert [j["kind"] for j in sender.jobs] == ["activity_start"]
    now[0] += 10
    service.record(scope, run["run_id"], "done", "complete")
    finished = now[0]
    now[0] += 3601
    register_activity(service, scope, ids, run["run_id"])
    service.drain_once()
    end = activities(sender)[-1]["payload"]["aps"]
    assert end["event"] == "end" and "alert" not in end
    assert end["dismissal-date"] == finished + 120


def test_end_obligation_recovers_an_expired_outbox_job(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    receipt = register_activity(service, scope, ids, run["run_id"])
    service.record(scope, run["run_id"], "failed", "failed")
    with service.store.transaction() as db:
        db.execute("UPDATE outbox SET expires_at=? WHERE subscription_id=?", (now[0] - 1, receipt["subscription_id"]))
    service.drain_once()
    assert len(activities(sender)) == 1
    assert activities(sender)[0]["payload"]["aps"]["event"] == "end"


def test_claim_without_ack_survives_restart_without_another_done_alert(delivery):
    from tui_gateway.mobile_push_store import PushStore
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    service.record(scope, run["run_id"], "done", "complete")
    claimed = service.store.claim()
    assert "alert" in claimed["payload"]["aps"]
    now[0] += 31
    restarted = PushStore(service.store.path, lambda: now[0])
    try:
        restarted.maintain()
        end = restarted.claim()
        assert end["payload"]["aps"]["event"] == "end"
        assert "alert" not in end["payload"]["aps"]
        restarted.finish(end, DeliveryResult("accepted"))
    finally:
        restarted.close()
    service.drain_once()
    assert sender.jobs == []


def test_late_registration_after_reply_alert_does_not_sound_again(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "done", "complete")
    service.drain_once()
    assert sender.jobs[0]["payload"]["aps"]["sound"] == "default"
    now[0] += 1
    register_activity(service, scope, ids, run["run_id"])
    service.drain_once()
    assert activities(sender)[0]["payload"]["aps"]["event"] == "update"
    assert "alert" not in activities(sender)[0]["payload"]["aps"]
    now[0] += 8
    service.drain_once()
    assert activities(sender)[-1]["payload"]["aps"]["event"] == "end"


def test_logout_removes_durable_cleanup_and_prevents_replay(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    service.record(scope, run["run_id"], "done", "failed")
    assert service.unregister("p", **ids) == 1
    now[0] += 9 * 3600
    service.drain_once()
    assert sender.jobs == []
