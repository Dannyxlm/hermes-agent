"""Exact task stops fence current authority and generations, without a room-wide stop."""

from types import SimpleNamespace
import threading

import pytest

from gateway import hosted_rooms as rooms
from gateway import hosted_room_driver as driver
from tui_gateway import server
from tui_gateway.hosted_room_service import HostedRoomService


@pytest.fixture
def room(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(rooms, "local_authority_gateway_id", lambda: "gateway-a")
    rooms.create_room(path, room_id="room-1", name="Fixture", members=[{"profile": "ops", "handle": "ops"}],
                      authority_gateway_id="gateway-a", now=90)
    for name in ("selected", "sibling"):
        driver.admit_task(path, identity(name), payload={"target_profile": "ops", "target_member_id": "ops",
            "prompt": "fixture work", "source_event_seq": 1}, clock=lambda: 100)
    return path


def identity(name="selected"):
    return driver.TaskIdentity("room-1", name, "thread-1", f"turn-{name}")


def guards(**overrides):
    return {"expected_execution_generation": 0, "expected_cancel_generation": 0,
            "expected_authority_gateway_id": "gateway-a", "expected_authority_epoch": 1, **overrides}


def exact_stop(room, **overrides):
    return driver.cancel_task_exact(room, identity(), cancel_id="stop-1", clock=lambda: 101, **guards(**overrides))


def test_exact_stop_cancels_only_selected_task_without_room_fence(room):
    stopped = exact_stop(room)
    assert stopped["status"] == "cancelled"
    assert stopped["cancel_generation"] == 1
    assert driver.get_task(room, identity("sibling"))["status"] == "queued"
    assert all(event["kind"] != "room.stop_requested" for event in rooms.read_events(room, room_id="room-1")["events"])


@pytest.mark.parametrize("change", [
    {"expected_execution_generation": 1}, {"expected_cancel_generation": 1},
    {"expected_authority_gateway_id": "gateway-b"}, {"expected_authority_epoch": 2},
])
def test_stale_coordinates_do_not_cancel_any_task(room, change):
    with pytest.raises(driver.DriverStateError):
        exact_stop(room, **change)
    assert all(task["status"] == "queued" for task in driver.list_tasks(room, room_id="room-1"))


def test_dispatch_cannot_fall_back_to_room_stop_with_partial_guard(room, monkeypatch):
    calls = []
    fake = SimpleNamespace(stop_room=lambda *args, **kwargs: calls.append("room") or 2)
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: fake)
    response = server.handle_request({"id": "stop", "method": "groups.stop", "params": {
        "room_id": "room-1", "cancel_id": "stop-1", "expected_task_id": "selected"}})
    assert "error" in response
    assert calls == []


def test_idempotent_exact_stop_keeps_generation(room):
    first = exact_stop(room)
    replay = exact_stop(room)
    assert replay["idempotent"] is True
    assert replay["cancel_generation"] == first["cancel_generation"]
    with pytest.raises(driver.StaleTaskError):
        driver.cancel_task_exact(room, identity(), cancel_id="different-stop", clock=lambda: 102, **guards())


def test_running_attempt_records_intent_without_claiming_it_stopped(room):
    lease = driver.acquire_lease(room, room_id="room-1", gateway_id="gateway-a", authority_epoch=1,
                                process_generation="fixture-process", ttl_seconds=30, clock=lambda: 100)
    attempt = driver.start_task(room, identity(), lease, expected_cancel_generation=0, clock=lambda: 100)
    with pytest.raises(driver.StaleTaskError):
        exact_stop(room)
    result = exact_stop(room, expected_execution_generation=attempt.execution_generation)
    assert result["status"] == "stopping"
    assert driver.get_task(room, identity("sibling"))["status"] == "queued"
    assert result["terminal_at"] is None


@pytest.mark.parametrize("field", ["expected_execution_generation", "expected_cancel_generation", "expected_authority_epoch"])
def test_boolean_generations_are_not_coordinates(room, field):
    with pytest.raises(driver.DriverValidationError):
        exact_stop(room, **{field: True})


def service_for(room):
    server_stub = SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())
    service = HostedRoomService(server_stub, db_path=room)
    service.runtime.clock = lambda: 101
    return service


def test_service_dispatch_and_stoppable_descriptors(room, monkeypatch):
    service = service_for(room)
    assert service.runtime.status()["running"] is False  # no test worker is launched
    status = service.status("room-1")
    assert {task["task_id"] for task in status["stoppable_tasks"]} == {"selected", "sibling"}
    assert all(task["execution_generation"] == 0 for task in status["stoppable_tasks"])
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: service)
    response = server.handle_request({"id": "stop", "method": "groups.stop", "params": {
        "room_id": "room-1", "cancel_id": "stop-1", "expected_task_id": "selected", **guards()}})
    assert response["result"]["stop_requested"] is True
    assert response["result"]["cancelled"] == 1
    assert response["result"]["task"] == {"task_id": "selected", "status": "cancelled",
        "execution_generation": 0, "cancel_generation": 1, "member_id": "ops"}
    assert [task["task_id"] for task in service.status("room-1")["stoppable_tasks"]] == ["sibling"]
    assert service.runtime.status()["running"] is False


def test_authority_change_between_service_preflight_and_transition_is_fenced(room, monkeypatch):
    service = service_for(room)
    cancel_exact = service.runtime.cancel_exact
    def change_authority(*args, **kwargs):
        rooms.claim_authority(room, room_id="room-1", expected_gateway_id="gateway-a", expected_epoch=1,
                              new_gateway_id="gateway-b", event_id="handoff", now=101)
        return cancel_exact(*args, **kwargs)
    monkeypatch.setattr(service.runtime, "cancel_exact", change_authority)
    with pytest.raises(driver.StaleLeaseError):
        service.stop_room_task("room-1", cancel_id="stop-1", expected_task_id="selected", **guards())
    assert all(task["status"] == "queued" for task in driver.list_tasks(room, room_id="room-1"))


def test_legacy_room_stop_remains_roomwide(room, monkeypatch):
    calls = []
    fake = SimpleNamespace(stop_room=lambda *args, **kwargs: calls.append("room") or 2)
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: fake)
    response = server.handle_request({"id": "stop", "method": "groups.stop", "params": {
        "room_id": "room-1", "cancel_id": "legacy-stop"}})
    assert response["result"] == {"cancelled": 2}
    assert calls == ["room"]
