"""Post-turn scheduling uses the real TUI turn lifecycle, not the agent finalizer."""
import threading
from types import SimpleNamespace

from tui_gateway import server
from tests.tui_gateway.test_auto_continue import turn_env, marker_home, _session
from tests.tui_gateway.test_between_turns_compaction import _fixture, _transcript

_REAL_THREAD = threading.Thread


def test_trigger_runs_only_after_delivery_and_settlement(turn_env, marker_home, tmp_path, monkeypatch):
    from agent.turn_finalizer import finalize_turn

    events = []
    # Construction starts a logging QueueListener: it must not run inline.
    monkeypatch.setattr(server.threading, "Thread", _REAL_THREAD)
    db, agent, base_session, before = _fixture(tmp_path, monkeypatch)
    from tests.tui_gateway.test_auto_continue import _InlineThread
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_emit", lambda kind, *a, **kw: events.append(kind))
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))
    monkeypatch.setattr(agent, "_sync_external_memory_for_turn", lambda **kw: events.append("memory.sync_turn"))
    monkeypatch.setattr(agent, "_spawn_background_review", lambda **kw: events.append("background_review"))
    monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a: None)
    agent.skip_background_review = False

    def run(message, **kwargs):
        # Exercise the actual agent finalizer, which owns persistence, memory
        # sync and review scheduling; the fake model supplies only its reply.
        return finalize_turn(
            agent, final_response="done", api_call_count=1, interrupted=False, failed=False,
            messages=before + [{"role": "user", "content": message}, {"role": "assistant", "content": "done"}],
            conversation_history=before, effective_task_id=agent.session_id, turn_id="turn-fixture",
            user_message=message, original_user_message=message, _should_review_memory=True,
            _turn_exit_reason="text_response(stop)")

    agent.run_conversation = run
    session = _session(**base_session)
    session["running"] = True

    def schedule(sid, s, result):
        assert s["running"] is False
        assert result["completed"] is True
        assert "message.complete" in events
        assert events.index("memory.sync_turn") < events.index("message.complete")
        assert events.index("background_review") < events.index("message.complete")
        assert any(m.get("content") == "done" for m in db.get_messages_as_conversation(agent.session_id))
        events.append("between_turns")

    monkeypatch.setattr(server, "_start_between_turns_compaction", schedule)
    try:
        server._run_prompt_submit("r", "sid", session, "go")
        assert events.count("between_turns") == 1
        assert events.index("between_turns") > events.index("message.complete")
    finally:
        agent.close()
        db.close()



def test_default_off_prompt_submit_never_schedules_compaction(turn_env, marker_home, monkeypatch):
    scheduled = []
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))
    monkeypatch.setattr(server, "_start_between_turns_compaction", lambda *a: scheduled.append(a))
    agent = SimpleNamespace(session_id="session-key", clear_interrupt=lambda: None,
                            run_conversation=lambda *a, **kw: {"completed": True, "final_response": "done", "messages": []})
    session = _session(agent=agent, running=True)
    assert server._run_prompt_submit("r", "sid", session, "go")
    assert scheduled == []
    assert "_between_turns_compaction" not in session


def test_real_compressor_commits_then_latch_prevents_second_attempt(tmp_path, monkeypatch):
    from agent.context_compressor import ContextCompressor
    db, agent, session, _ = _fixture(tmp_path, monkeypatch)
    messages = [{"role": role, "content": f"{i} " + "x" * 10000}
                for i in range(12) for role in ("user", "assistant")]
    db.replace_messages(agent.session_id, messages)
    session["history"] = db.get_messages_as_conversation(agent.session_id)
    cc = ContextCompressor(model="test/model", quiet_mode=True, config_context_length=1_000_000,
                           threshold_tokens_cap=500_000, protect_first_n=1, protect_last_n=2)
    cc.bind_session_state(session_db=db, session_id=agent.session_id)
    monkeypatch.setattr(ContextCompressor, "_generate_summary", lambda self, *a, **kw: "Earlier work is complete.")
    agent.context_compressor = cc
    try:
        job = server._start_between_turns_compaction("sid", session, {"completed": True})
        assert job is not None
        assert job.done.wait(10)
        assert session["history_version"] == 2
        assert len(session["history"]) < len(messages)
        assert _transcript(session["history"]) == _transcript(db.get_messages_as_conversation(agent.session_id))
        assert agent.context_compressor.awaiting_real_usage_after_compression is True
        assert server._start_between_turns_compaction("sid", session, {"completed": True}) is None
    finally:
        agent.close()
        db.close()


def test_arriving_prompt_runs_real_tui_turn_once_while_engine_still_blocked(
    turn_env, marker_home, tmp_path, monkeypatch,
):
    import time
    monkeypatch.setattr(server.threading, "Thread", _REAL_THREAD)
    db, agent, base_session, before = _fixture(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    original = agent.context_compressor
    def slow_engine(messages, **kwargs):
        started.set()
        assert release.wait(10)
        return messages
    original.compress.side_effect = slow_engine
    def start_work(target, *, name, session=None):
        if name.startswith("between-turns-compaction"):
            thread = _REAL_THREAD(target=target, daemon=True)
            thread.start()
            return thread
        target()
        return SimpleNamespace(is_alive=lambda: False)
    monkeypatch.setattr(server, "_start_session_work", start_work)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (threading.Event(), SimpleNamespace(join=lambda: None)))
    session = _session(agent=agent, running=False, **{k: v for k, v in base_session.items() if k not in {"agent", "running"}})
    calls = []
    def run(prompt, **kwargs):
        calls.append(prompt)
        # The real agent normally persists these before the gateway absorbs its result.
        db.append_message(agent.session_id, "user", "next")
        db.append_message(agent.session_id, "assistant", "answer")
        return {"completed": True, "final_response": "answer",
                "messages": db.get_messages_as_conversation(agent.session_id)}
    agent.run_conversation = run
    job = server._start_between_turns_compaction("sid", session, {"completed": True})
    try:
        assert started.wait(5)
        # Disable rescheduling after the new reply so this checks exactly one abandoned attempt.
        agent.compression_between_turns = False
        begin = time.monotonic()
        with server._session_turn_admission(session):
            session["running"] = True
        assert server._run_prompt_submit("r", "sid", session, "next")
        assert time.monotonic() - begin < 2
        assert calls == ["next"]
        assert not job.done.is_set()
        assert sum(m.get("content") == "next" for m in session["history"]) == 1
        assert len(db.get_messages_as_conversation(agent.session_id)) == len(before) + 2
        release.set()
        assert job.done.wait(5)
        assert agent.context_compressor is original
        assert sum(m.get("content") == "next" for m in session["history"]) == 1
    finally:
        release.set()
        job.done.wait(5)
        agent.close()
        db.close()
