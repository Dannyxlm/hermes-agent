"""Canonical preview vault operations fail before reaching any session viewer."""
import json
from types import SimpleNamespace

import pytest

from gateway import local_desktop_ui as ui
from gateway import session_server_requests as requests
from gateway.session_events import SessionEvents


@pytest.mark.parametrize("operation", ["open", "evaluate", "close"])
def test_canonical_preview_vault_never_fans_out(operation):
    class Viewer:
        def __init__(self, answers=False):
            self.frames = []
            self.answers = answers

        def write(self, frame):
            self.frames.append(frame)
            if self.answers and frame.get("method") == "preview.act":
                requests.resolve({"id": frame["id"], "result": {
                    "value": json.dumps({"success": True, "target": "T", "result": 1})}})
            return True

        def close(self):
            pass

    events = SessionEvents()
    desktop, second = Viewer(True), Viewer()
    events.fanout.attach(desktop)
    events.fanout.attach(second)
    sid = "canonical-vault-" + operation
    authority = SimpleNamespace(sessions={sid: SimpleNamespace(event_stream=events)})
    turn = SimpleNamespace(_approval_owner=(authority, sid, 1))
    agent = SimpleNamespace(_interrupt_requested=False)
    result = json.loads(ui._preview_action(turn, agent, {"action": "vault", "vault": {
        "operation": operation, "target": "T", "expression": "SYNTHETIC-SECRET-CANARY"}}))
    assert desktop.frames == []
    assert second.frames == []
    assert result["success"] is False
    assert "pinned" in result["error"].lower()
