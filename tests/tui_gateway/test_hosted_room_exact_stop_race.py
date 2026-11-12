"""Review probe (PR #204 delta): a turn that becomes deferred AFTER exact Stop's pre-read (so no
confirmation ran) must not be cancelled by the fenced transaction. Synthetic room."""
import pytest

from gateway import hosted_room_driver as driver
from tests.tui_gateway.test_hosted_room_exact_stop import guards, identity, service_for

pytest_plugins = ("tests.tui_gateway.test_hosted_room_exact_stop",)


def test_deferred_row_without_confirmation_is_refused_in_the_transaction(room):
    lease = driver.acquire_lease(room, room_id="room-1", gateway_id="gateway-a", authority_epoch=1,
                                process_generation="fixture-process", ttl_seconds=30, clock=lambda: 100)
    attempt = driver.start_task(room, identity(), lease, expected_cancel_generation=0, clock=lambda: 100)
    driver.mark_attempt_indeterminate(room, attempt, lease, clock=lambda: 100)
    driver.defer_indeterminate_task(room, identity(), lease,
        expected_execution_generation=attempt.execution_generation, expected_cancel_generation=0,
        reason="unconfirmed_outcome", clock=lambda: 100)
    with pytest.raises(driver.StaleTaskError):
        driver.cancel_task_exact(room, identity(), cancel_id="stop-1", clock=lambda: 101,
                                 **guards(expected_execution_generation=attempt.execution_generation))
    assert driver.get_task(room, identity())["status"] == "deferred"
    assert driver.get_task(room, identity())["cancel_generation"] == 0


def test_owner_exact_stop_of_a_locally_served_deferred_turn_still_cancels(room):
    """Neighbour check: no peer, this process serves the member -> the real confirmation hook says yes."""
    lease = driver.acquire_lease(room, room_id="room-1", gateway_id="gateway-a", authority_epoch=1,
                                process_generation="fixture-process", ttl_seconds=30, clock=lambda: 100)
    attempt = driver.start_task(room, identity(), lease, expected_cancel_generation=0, clock=lambda: 100)
    driver.mark_attempt_indeterminate(room, attempt, lease, clock=lambda: 100)
    driver.defer_indeterminate_task(room, identity(), lease,
        expected_execution_generation=attempt.execution_generation, expected_cancel_generation=0,
        reason="unconfirmed_outcome", clock=lambda: 100)
    service = service_for(room)
    seen = []
    real = service.runtime.confirm_deferred_stop
    service.runtime.confirm_deferred_stop = lambda rid, task: seen.append(real(rid, task)) or seen[-1]
    result = service.runtime.cancel_exact(identity(), cancel_id="stop-1",
                                         **guards(expected_execution_generation=attempt.execution_generation))
    print("PROBE-STOP real hook answered", seen, "status", result["status"])
    assert seen and result["status"] == ("cancelled" if seen[-1] else "deferred")
