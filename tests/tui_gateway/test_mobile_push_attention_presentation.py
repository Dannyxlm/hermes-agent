"""An attention event has one interrupting surface on a device, preferring the request-sheet alert."""

import uuid

import pytest

from tests.tui_gateway.test_mobile_push import delivery as delivery  # noqa: PLC0414


@pytest.mark.parametrize("status", ["waitingForApproval", "waitingForClarification"])
@pytest.mark.parametrize("activity_first", [False, True])
def test_attention_has_one_presentation_when_alert_and_activity_cover_event(delivery, status, activity_first):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    if not activity_first:
        service.register("p", scope, **ids, token="aa" * 32, environment="production")
    service.register("p", scope, **ids, token="cc" * 32, environment="production", kind="activity",
                     activity_id="activity", run_id=run["run_id"])
    if activity_first:
        service.register("p", scope, **ids, token="aa" * 32, environment="production")
    service.record(scope, run["run_id"], "ask", status)
    now[0] += 1
    service.drain_once()
    presentations = [job for job in sender.jobs if "alert" in job["payload"]["aps"]]
    assert len(presentations) == 1
    assert presentations[0]["kind"] == "alert"
    [activity] = [job for job in sender.jobs if job["kind"] == "activity"]
    assert activity["payload"]["aps"]["content-state"]["status"] == status
    assert "alert" not in activity["payload"]["aps"]


def test_activity_keeps_attention_cue_when_no_ordinary_job_covers_event(delivery):
    service, sender, now, ids, scope = delivery
    run = service.start_run(scope)
    service.register("p", scope, **ids, token="cc" * 32, environment="production", kind="activity",
                     activity_id="activity", run_id=run["run_id"])
    service.record(scope, run["run_id"], "ask", "waitingForApproval")
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    now[0] += 1
    service.drain_once()
    assert [job["kind"] for job in sender.jobs if "alert" in job["payload"]["aps"]] == ["activity"]


@pytest.mark.parametrize("adopt", [False, True])
def test_activity_only_attention_reservation_survives_retry_and_reopen(delivery, adopt):
    import hashlib

    from tui_gateway.methods_browser_control import _principal_digest
    from tui_gateway.mobile_push_adoption import adopt_legacy_installation
    from tui_gateway.mobile_push_provider import DeliveryResult
    from tui_gateway.mobile_push_store import PushStore

    identity = {"provider": "test-auth", "user_id": "test-user"}
    legacy = "principal:dashboard:" + hashlib.sha256(b"test-auth\0test-user").hexdigest()[:32]
    service, _sender, now, ids, scope = delivery
    run = service.start_run(scope)
    service.register(legacy, scope, **ids, token="cc" * 32, environment="production", kind="activity",
                     activity_id="activity", run_id=run["run_id"])
    service.record(scope, run["run_id"], "ask", "waitingForApproval")
    service.register(legacy, scope, **ids, token="aa" * 32, environment="production")
    first = service.store.claim()
    assert first is not None and "alert" in first["payload"]["aps"]
    service.store.finish(first, DeliveryResult("retry"))
    if adopt:
        adopt_legacy_installation(service.store, identity, _principal_digest(identity), ids["installation_id"])
    now[0] += 31
    reopened = PushStore(service.store.path, lambda: now[0])
    try:
        retry = reopened.claim()
        assert retry is not None and retry["job_id"] == first["job_id"]
        assert retry["principal"] == (_principal_digest(identity) if adopt else legacy)
        assert "alert" not in retry["payload"]["aps"]
        assert retry["payload"]["aps"]["content-state"]["status"] == "waitingForApproval"
    finally:
        reopened.close()


def test_attention_arbitration_is_per_event_and_physical_device_not_connection(delivery):
    service, sender, now, ids, scope = delivery
    other_connection = {**ids, "connection_id": str(uuid.uuid4())}
    run = service.start_run(scope)
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    service.register("p", scope, **other_connection, token="cc" * 32, environment="production", kind="activity",
                     activity_id="activity", run_id=run["run_id"])
    for event in ("first", "second"):
        service.record(scope, run["run_id"], event, "waitingForApproval")
        now[0] += 1
        service.drain_once()
        service.record(scope, run["run_id"], event + "-resumed", "thinking")
    presentations = [job for job in sender.jobs if "alert" in job["payload"]["aps"]]
    assert [job["kind"] for job in presentations] == ["alert", "alert"]
    other = {**ids, "installation_id": str(uuid.uuid4())}
    service.register("p", scope, **other, token="ee" * 32, environment="production")
    service.record(scope, run["run_id"], "third", "waitingForApproval")
    now[0] += 1
    service.drain_once()
    assert len([job for job in sender.jobs if "alert" in job["payload"]["aps"]]) == 4
