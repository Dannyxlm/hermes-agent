"""``session.info`` as a read-only JSON-RPC method (Hermex Gate C G-3).

Native Hermex reads ``session.info`` back after its per-chat "Bypass approvals" toggle writes
``config.set yolo`` (scope=session), and again on every chat open. The gateway only ever *emitted*
``session.info`` as an event, so each read answered JSON-RPC -32601 and the toggle reported a failure
for a change that had applied. The method returns the same ``_session_info`` snapshot the event carries.
"""

from __future__ import annotations

import threading
import types
from pathlib import Path
from unittest.mock import patch

import hermes_yaml as yaml
import pytest

import tui_gateway.server as server
from tui_gateway.contracts.common import SessionLiveInfo
from tui_gateway.contracts.registry import METHODS


def _write_cfg(home: Path, approvals: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"approvals": {"mode": approvals}}), encoding="utf-8")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    launch, worker = tmp_path / "launch", tmp_path / "profiles" / "worker"
    _write_cfg(launch, "manual")
    _write_cfg(worker, "manual")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    return launch, worker


@pytest.fixture
def emitted(monkeypatch):
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None, *a, **k: events.append((event, sid, payload)))
    return events


@pytest.fixture(autouse=True)
def _clear_session_yolo():
    from tools.approval import clear_session

    yield
    for key in ("session-key", "lazy-key", "worker-key"):
        clear_session(key)


def _session(agent=None, *, key: str = "session-key", **extra) -> dict:
    return {
        "agent": agent, "session_key": key, "history": [], "history_lock": threading.Lock(),
        "history_version": 0, "running": False, "attached_images": [], "image_counter": 0,
        "cols": 80, "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all",
        "cwd": str(Path.cwd()), **extra,
    }


def _rpc(method: str, params: dict, rid: str = "1") -> dict:
    return server.handle_request({"id": rid, "method": method, "params": params})


def _agent() -> types.SimpleNamespace:
    return types.SimpleNamespace(session_id="session-key", model="fixture-model", provider="fixture-provider", tools=[])


def test_session_info_is_a_registered_method_with_a_contract(homes, emitted):
    """The exact app call (session_id + profile) is answered, not -32601."""
    assert "session.info" in server._methods
    assert METHODS["session.info"].result is SessionLiveInfo
    with patch.dict(server._sessions, {"sid": _session(_agent())}, clear=False):
        resp = _rpc("session.info", {"session_id": "sid", "profile": "default"})
    assert "error" not in resp, resp
    info = resp["result"]
    assert info["yolo"] is False
    assert info["approval_mode"] == "manual"
    assert info["stored_session_id"] == "session-key"
    SessionLiveInfo.model_validate(info)


def test_bypass_toggle_readback_confirms_on_then_off(homes, emitted):
    """Hermex setApprovalBypass: config.set yolo (scope=session) then session.info; the readback matches
    the session's real flag both ways, and equals the session.info event config.set pushed."""
    from tools.approval import is_session_yolo_enabled

    with patch.dict(server._sessions, {"sid": _session(_agent())}, clear=False):
        for value, expected in (("1", True), ("0", False)):
            emitted.clear()
            wrote = _rpc("config.set", {"session_id": "sid", "profile": "default", "scope": "session",
                                        "key": "yolo", "value": value})
            assert wrote["result"]["value"] == value, wrote
            read = _rpc("session.info", {"session_id": "sid", "profile": "default"}, rid="2")
            assert "error" not in read, read
            assert read["result"]["yolo"] is expected
            assert is_session_yolo_enabled("session-key") is expected
            pushed = [p for (e, sid, p) in emitted if e == "session.info" and sid == "sid"]
            assert pushed, "config.set yolo must still push the session.info event"
            for field in ("yolo", "approval_mode", "model", "provider", "stored_session_id", "profile_name"):
                assert read["result"][field] == pushed[-1][field], field


def test_read_is_side_effect_free_for_a_not_yet_built_session(homes, emitted):
    """A lazy (agent-less) session answers the lazy route plus the approval state, never builds or
    waits on the agent, and emits nothing."""
    session = _session(None, key="lazy-key", model_override={"model": "picked-model", "provider": "picked"})
    with patch.dict(server._sessions, {"lazy": session}, clear=False), \
            patch.object(server, "_schedule_agent_build", side_effect=AssertionError("must not build")), \
            patch.object(server, "_wait_agent", side_effect=AssertionError("must not wait")):
        _rpc("config.set", {"session_id": "lazy", "scope": "session", "key": "yolo", "value": "1"})
        emitted.clear()
        resp = _rpc("session.info", {"session_id": "lazy"})
    assert "error" not in resp, resp
    info = resp["result"]
    assert info["yolo"] is True
    assert info["approval_mode"] == "manual"
    assert info["lazy"] is True
    assert info["model"] == "picked-model"
    assert info["stored_session_id"] == "lazy-key"
    assert session["agent"] is None
    assert emitted == []


def test_approval_mode_is_read_from_the_session_profile(homes, emitted):
    """approvals.mode=off in the session's profile reads back yolo, while the launch profile stays manual."""
    launch, worker = homes
    _write_cfg(worker, "off")
    session = _session(None, key="worker-key", profile_home=str(worker))
    with patch.dict(server._sessions, {"s-worker": session, "s-launch": _session(None, key="session-key")},
                    clear=False):
        worker_info = _rpc("session.info", {"session_id": "s-worker"})["result"]
        launch_info = _rpc("session.info", {"session_id": "s-launch"}, rid="2")["result"]
    assert (worker_info["approval_mode"], worker_info["yolo"]) == ("off", True)
    assert (launch_info["approval_mode"], launch_info["yolo"]) == ("manual", False)
    # Hermex rejects a readback whose profile_name differs from the chat's profile id.
    assert worker_info["profile_name"] == "worker"


def test_stale_runtime_id_answers_session_not_found(homes, emitted):
    with patch.dict(server._sessions, {}, clear=True):
        resp = _rpc("session.info", {"session_id": "reaped-sid", "profile": "default"})
    assert resp["error"]["code"] == 4001, resp


def test_session_info_runs_off_the_socket_reader():
    """_session_info can wait up to 0.5 s on the update check and shells out to git: keep it off the
    inline read loop (approval.respond / session.interrupt must stay readable)."""
    assert "session.info" in server._LONG_HANDLERS
