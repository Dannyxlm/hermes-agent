"""Paged phone opens skip display restore; optional warm info cannot serialize resumes."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def resume_store(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="tui")
    db.append_message("root", "user", "fixture question")
    db.append_message("root", "assistant", "fixture answer")
    db.end_session("root", "compression")
    db.create_session("tip", source="tui", parent_session_id="root")
    db.append_message("tip", "user", "fixture continuation")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_profile_session_db", lambda home: (db, False))
    monkeypatch.setattr(server, "_profile_home", lambda profile: None)
    monkeypatch.setattr(server, "_profile_default_cwd", lambda home: str(tmp_path))
    monkeypatch.setattr(server, "_default_session_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *args: None)
    monkeypatch.setattr(server, "_lazy_resume_info", lambda cwd, **kw: {"cwd": cwd, "lazy": True})
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    yield db
    for session in server._sessions.values():
        if ready := session.get("resume_history_ready"):
            assert ready.wait(5)
    db.close()


def resume(target="tip", **params):
    return server.handle_request({"id": "resume", "method": "session.resume",
                                  "params": {"session_id": target, **params}})


@pytest.mark.parametrize("source,omit,expected_display_reads", [
    ("tui", True, 0), ("tui", False, 1), ("desktop", True, 0),
])
def test_deferred_open_restores_display_only_when_consumed(resume_store, monkeypatch, source, omit, expected_display_reads):
    display = Mock(wraps=resume_store.get_resume_conversations)
    ancestors = Mock(wraps=resume_store.get_ancestor_display_prefix)
    monkeypatch.setattr(resume_store, "get_resume_conversations", display)
    monkeypatch.setattr(resume_store, "get_ancestor_display_prefix", ancestors)
    response = resume(source=source, defer_history=True, omit_messages=omit)
    assert "result" in response, response
    session = server._sessions[response["result"]["session_id"]]
    assert session["resume_history_ready"].wait(5)
    assert not session.get("resume_history_error")
    assert display.call_count == expected_display_reads
    assert ancestors.call_count == expected_display_reads
    assert session["history"][-1]["content"] == "fixture continuation"
    assert response["result"]["messages"] == []
    if omit:
        assert session["display_history_prefix"] == []
        assert session["resume_message_count"] == 1
    else:
        assert session["display_history_prefix"]


@pytest.mark.parametrize("eager_race", [False, True])
def test_warm_info_does_not_hold_up_another_sessions_resume(resume_store, monkeypatch, tmp_path, eager_race):
    entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
    results = {}
    records = {}
    for key in ("first", "second"):
        resume_store.create_session(key, source="tui")
        record = server._deferred_session_record(key, cols=80, cwd=str(tmp_path), history=[], lease=None)
        record["agent"] = SimpleNamespace(session_id=key)
        records[key] = record
        if key != "first" or not eager_race:
            server._sessions[key + "-runtime"] = record

    def concurrent_winner(*args, **kwargs):
        server._sessions["first-runtime"] = records["first"]
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(server, "_make_agent_in_context", concurrent_winner)

    def slow_info(agent, session=None):
        if agent.session_id == "first":
            entered.set()
            assert release.wait(10), "test did not release optional info"
        return {"model": "fixture-model", "stored_session_id": agent.session_id}

    monkeypatch.setattr(server, "_session_info", slow_info)

    def run(key):
        results[key] = resume(key, omit_messages=True, defer_history=True, eager_build=eager_race and key == "first")
        if key == "second":
            second_done.set()

    first = threading.Thread(target=run, args=("first",))
    second = threading.Thread(target=run, args=("second",))
    first.start()
    try:
        assert entered.wait(5), "warm resume did not reach optional info"
        second.start()
        assert second_done.wait(3), "another session's resume waited on optional rich info"
        assert results["second"]["result"]["session_id"] == "second-runtime"
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert results["first"]["result"]["session_id"] == "first-runtime"
    assert results["first"]["result"]["info"]["model"] == "fixture-model"
