"""Actual input controls must preempt opportunistic jobs, not demote them to queue."""
import contextlib
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tests.tui_gateway.test_auto_continue import _patch_local_interrupt
from tests.tui_gateway.test_between_turns_compaction import _fixture


@pytest.mark.parametrize("control", ["steer", "redirect", "interrupt"])
def test_input_control_preempts_existing_job(tmp_path, monkeypatch, control):
    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    seen = []

    def blocked(messages, **kwargs):
        started.set()
        assert release.wait(10)
        return messages

    agent.context_compressor.compress.side_effect = blocked
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_session_db", lambda session: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_clear_pending", lambda *a: None)
    _patch_local_interrupt(monkeypatch, session)
    agent._supports_active_turn_redirect = True
    for name in ("steer", "redirect", "interrupt"):
        monkeypatch.setattr(agent, name, lambda *a, name=name, **kw: seen.append((name, a)) or True)
    monkeypatch.setattr(agent, "hard_interrupt", lambda **kw: seen.append(("interrupt", ())))
    job = server._start_between_turns_compaction("sid", session, {"completed": True})
    try:
        assert started.wait(5)
        session["running"] = True
        session["_run_thread"] = SimpleNamespace(is_alive=lambda: True)
        if control == "interrupt":
            response = server._methods["session.interrupt"]("r", {"session_id": "sid"})
            assert "error" not in response
        else:
            mode = "steer" if control == "steer" else "interrupt"
            monkeypatch.setattr(server, "_load_busy_input_mode", lambda: mode)
            response = server._handle_busy_submit("r", "sid", session, "correction", None)
            assert response["result"]["status"] == ("steered" if control == "steer" else "redirected")
        assert seen == [(control, () if control == "interrupt" else ("correction",))]
        assert job.fence.cancel_reason == "preempted_by_turn"
        assert not job.done.is_set()
        assert not session.get("queued_prompt")
        release.set()
        assert job.done.wait(5)
        assert session["history"] == before
        assert db.get_messages_as_conversation(agent.session_id) == before
    finally:
        release.set()
        job.done.wait(5)
        agent.close()
        db.close()
