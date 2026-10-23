"""Both fork mobile extensions and upstream wire additions remain accepted."""

from tui_gateway.contracts.events import MessageStartPayload
from tui_gateway.contracts.groups_bot_relay import GroupsCapabilitiesResult, RoomDriverStatus
from tui_gateway.contracts.liveness import ClientCapabilitiesParams, GatewayPingResult
from tui_gateway.contracts.sessions import SessionEventsSinceParams


def test_mobile_and_native_desktop_capabilities_coexist():
    params = ClientCapabilitiesParams.model_validate({
        "server_requests": True, "session_change_cursor": True, "desktop_local": True,
    })
    assert params.session_change_cursor and params.desktop_local and params.server_requests
    assert GatewayPingResult.model_validate({"ok": True}).ok


def test_chunked_replay_keeps_epoch_hint():
    params = SessionEventsSinceParams.model_validate({
        "session_id": "fixture", "chunked_response": True, "replay_epoch": "old-process",
    })
    assert params.chunked_response and params.replay_epoch == "old-process"


def test_notice_presentation_coexists_with_automation_header():
    payload = MessageStartPayload.model_validate({
        "presentation": {"display_kind": "background_notice", "display_text": "done"},
        "display_kind": "async_result", "display_text": "completed", "text": "model input",
    })
    assert payload.presentation is not None
    assert payload.presentation.display_text == "done"
    assert payload.text == "model input"


def test_hosted_room_activity_keeps_generation_fenced_stop_handles():
    status = RoomDriverStatus.model_validate({
        "running": True, "working": True, "blocked": False, "counts": {},
        "pending_actions": [], "peer_routes": [],
        "member_activity": [{"member_id": "a", "status": "running"}],
        "stoppable_tasks": [{"task_id": "t", "execution_generation": 2,
                             "cancel_generation": 1, "status": "running", "member_id": "a"}],
    })
    assert status.member_activity[0].member_id == status.stoppable_tasks[0].member_id
    assert status.stoppable_tasks[0].execution_generation == 2
    assert {"feature_versions", "seatable_profiles"} <= GroupsCapabilitiesResult.model_fields.keys()
