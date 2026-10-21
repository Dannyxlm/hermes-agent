"""Finish sound follows current ActivityKit ownership, not historical registration."""
import pytest

from tests.tui_gateway.test_mobile_push import delivery as delivery  # noqa: PLC0414
from tests.tui_gateway.test_mobile_push_lifecycle import activities, register_activity
from tui_gateway.mobile_push_provider import DeliveryResult


@pytest.mark.parametrize("loss", ["unregister", "invalid", "lifetime"])
@pytest.mark.parametrize("queued", [False, True])
def test_reply_keeps_sound_after_activity_loss(delivery, loss, queued):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    # Keep a public claimed update available for an invalid-token acknowledgement.
    now[0] += 10
    update = service.store.claim() if loss == "invalid" else None
    if loss == "invalid":
        assert update is not None and update["kind"] == "activity"
    if loss == "lifetime":
        now[0] += 8 * 3600
    if queued:
        service.record(scope, run["run_id"], "done", "complete")
    if loss == "unregister":
        assert service.unregister("p", **ids, kind="activity") == 1
    elif loss == "invalid":
        service.store.finish(update, DeliveryResult("invalid", "token_invalid"))
    else:
        service.drain_once()
        ends = [j for j in activities(sender) if j["payload"]["aps"].get("event") == "end"]
        assert len(ends) == 1
        if not queued:
            assert service.current_run(scope)["status"] == "starting"
    if not queued:
        service.record(scope, run["run_id"], "done", "complete")
    service.drain_once()
    alerts = [j["payload"]["aps"] for j in sender.jobs if j["kind"] == "alert"]
    assert len(alerts) == 1
    assert alerts[0].get("sound") == "default"
    assert alerts[0].get("interruption-level") != "passive"
    assert not [j for j in activities(sender) if "alert" in j["payload"]["aps"]]


def test_claimed_done_remains_reserved_after_unregister(delivery):
    service, sender, _now, ids, scope = delivery
    run = service.start_run(scope)
    register_activity(service, scope, ids, run["run_id"])
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    service.record(scope, run["run_id"], "done", "complete")
    done = service.store.claim()
    assert done is not None and done["payload"]["aps"].get("alert", {}).get("sound") == "default"
    # No acknowledgement: delivery is uncertain, so unregister must not replay sound.
    service.unregister("p", **ids, kind="activity")
    service.drain_once()
    alerts = [j["payload"]["aps"] for j in sender.jobs if j["kind"] == "alert"]
    assert len(alerts) == 1
    assert alerts[0].get("interruption-level") == "passive"
    assert "sound" not in alerts[0]
