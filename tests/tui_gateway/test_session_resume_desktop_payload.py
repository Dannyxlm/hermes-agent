"""Live resume payload parity with the Desktop fixture."""

import json
import threading
import types
from pathlib import Path

from tests.tui_gateway.test_protocol import _restore_stdout, server as server  # noqa: PLC0414 - pytest fixture


def test_session_resume_active_turn_payload_matches_desktop_fixture(server, monkeypatch):
    """A live resume serializes the exact timer payload consumed by Desktop."""
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "session-resume-active-turn.json").read_text(
            encoding="utf-8-sig"
        )
    )

    class _DB:
        def get_session(self, session_id):
            return {"id": session_id}

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, session_id):
            return session_id

    active_turn = {
        "assistant": "partial answer",
        "started_at": fixture["turn_started_at"],
        "streaming": True,
        "user": "current prompt",
    }
    server._sessions[fixture["session_id"]] = {
        "agent": types.SimpleNamespace(session_id=fixture["session_key"]),
        "created_at": fixture["started_at"],
        "history": [{"content": "earlier prompt", "role": "user"}],
        "history_lock": threading.Lock(),
        "inflight_turn": active_turn,
        "running": True,
        "session_key": fixture["session_key"],
    }
    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_session_info", lambda _agent, _session=None: fixture["info"])

    # JSON round-trip the real RPC envelope: the desktop fixture must stay
    # faithful to what the gateway actually serializes, not a copied shape.
    response = json.loads(
        json.dumps(
            server.handle_request(
                {
                    "id": "resume-running",
                    "method": "session.resume",
                    "params": {"session_id": fixture["session_key"]},
                }
            )
        )
    )
    result = response["result"]

    assert result["running"] is True
    assert result["turn_started_at"] == active_turn["started_at"]
    snapshot = result.pop("stream_snapshot")
    assert isinstance(snapshot["epoch"], str) and snapshot["epoch"]
    assert snapshot == {
        "baseline_seq": 0,
        "epoch": snapshot["epoch"],
        "session_id": fixture["session_id"],
        "stored_session_id": fixture["session_key"],
        "stream": None,
    }
    assert result == fixture
