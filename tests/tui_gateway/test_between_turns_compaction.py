"""Between-turns work is opportunistic: arriving user input always wins."""
import copy
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tui_gateway import server


def _fixture(tmp_path, monkeypatch, *, in_place=True):
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from agent.context_compressor import SUMMARY_PREFIX

    db = SessionDB(db_path=tmp_path / "state.db")
    parent = "between-turns-parent"
    db.create_session(parent, source="cli")
    for i in range(12):
        db.append_message(parent, "user", f"question {i} " + "x" * 80)
        db.append_message(parent, "assistant", f"answer {i} " + "y" * 80)
    before = db.get_messages_as_conversation(parent)
    agent = AIAgent(api_key="fixture-only", base_url="http://127.0.0.1:9/v1",
                    model="test/model", platform="cli", quiet_mode=True,
                    session_db=db, session_id=parent, skip_context_files=True,
                    skip_memory=True, enabled_toolsets=[])
    compressor = MagicMock()
    compressor.compress.return_value = [
        {"role": "user", "content": SUMMARY_PREFIX + " earlier turns"}, before[-1].copy()]
    compressor.compression_count = 1
    compressor.threshold_tokens = 500_000
    compressor.summary_target_ratio = 0.2
    compressor.last_compression_rough_tokens = 0
    compressor.last_prompt_tokens = compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = compressor._last_summary_auth_failure = False
    compressor._last_aux_model_failure_model = compressor._last_aux_model_failure_error = None
    compressor.awaiting_real_usage_after_compression = False
    compressor.get_active_compression_failure_cooldown.return_value = None
    agent.context_compressor = compressor
    agent.compression_enabled = agent.compression_between_turns = True
    agent.compression_between_turns_ratio = 0.9
    agent.compression_in_place = in_place
    agent._compression_feasibility_checked = True
    agent._cached_system_prompt = "fixture prompt"
    monkeypatch.setattr(agent, "_build_system_prompt", lambda *a, **kw: "fixture prompt")
    from agent import turn_context

    def pressure(agent, messages, system):
        agent._request_pressure_anchored = True
        return 450_000

    monkeypatch.setattr(turn_context, "_preflight_request_tokens", pressure)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_emit_session_info", lambda *a, **kw: None)
    session = {"agent": agent, "history_lock": threading.Lock(), "history": copy.deepcopy(before),
               "history_version": 1, "running": False, "session_key": parent, "cwd": str(tmp_path)}
    return db, agent, session, before


def _transcript(messages):
    return [(m["role"], m.get("content")) for m in messages]


@pytest.mark.parametrize("in_place", [True, False])
def test_completed_turn_compacts_and_next_turn_uses_committed_history(tmp_path, monkeypatch, in_place):
    db, agent, session, before = _fixture(tmp_path, monkeypatch, in_place=in_place)
    try:
        job = server._start_between_turns_compaction("sid", session, {"completed": True})
        assert job is not None
        assert job.done.wait(10)
        assert not session["running"]
        assert session["history_version"] == 2
        assert len(session["history"]) < len(before)
        assert _transcript(session["history"]) == _transcript(db.get_messages_as_conversation(agent.session_id))
        assert session["session_key"] == agent.session_id
        assert (agent.session_id == "between-turns-parent") is in_place
        agent.context_compressor.awaiting_real_usage_after_compression = True
        assert server._start_between_turns_compaction("sid", session, {"completed": True}) is None
        with server._session_turn_admission(session) as admitted:
            assert admitted
            assert _transcript(session["history"]) == _transcript(db.get_messages_as_conversation(agent.session_id))
    finally:
        agent.close()
        db.close()


def test_rebuilt_system_prompt_is_a_fresh_build_not_nested(tmp_path, monkeypatch):
    """The commit-boundary rebuild must equal a fresh build. Handing the cached prompt to
    _compress_context as the caller system message nested the whole old prompt inside the
    new one, so each between-turns compaction doubled it (identity, skills, AGENTS.md twice)."""
    db, agent, session, _before = _fixture(tmp_path, monkeypatch)
    monkeypatch.delattr(agent, "_build_system_prompt")  # the real builder, not the fixture's constant
    fresh = agent._cached_system_prompt = agent._build_system_prompt(None)
    try:
        job = server._start_between_turns_compaction("sid", session, {"completed": True})
        assert job is not None
        assert job.done.wait(10)
        assert session["history_version"] == 2
        rebuilt = session["agent"]._cached_system_prompt
        assert rebuilt.count(fresh[:200]) == 1
        assert len(rebuilt) < 1.5 * len(fresh)
    finally:
        agent.close()
        db.close()


@pytest.mark.parametrize("guard", ["disabled", "compression_disabled", "interrupted", "failed", "incomplete", "cooldown", "latch", "native", "below", "threshold", "floor", "unanchored", "running", "queue", "lock", "compute_host"])
def test_does_not_start_when_ineligible(tmp_path, monkeypatch, guard):
    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    result = {"completed": True}
    cc = agent.context_compressor
    if guard == "disabled": agent.compression_between_turns = False
    if guard == "compression_disabled": agent.compression_enabled = False
    if guard == "interrupted": result["interrupted"] = True
    if guard == "failed": result["failed"] = True
    if guard == "incomplete": result["completed"] = False
    if guard == "cooldown": cc.get_active_compression_failure_cooldown.return_value = {"remaining_seconds": 10}
    if guard == "latch": cc.awaiting_real_usage_after_compression = True
    if guard == "native": agent.api_mode = "codex_app_server"
    if guard == "floor": cc.last_compression_rough_tokens = 400_000
    if guard == "compute_host": session["_compute_host_active"] = True
    if guard == "running": session["running"] = True
    if guard == "queue": session["queued_prompt"] = {"text": "next"}
    if guard == "lock": assert db.try_acquire_compression_lock(agent.session_id, "another")
    if guard in {"below", "threshold", "unanchored"}:
        from agent import turn_context
        def pressure(a, messages, system):
            a._request_pressure_anchored = guard != "unanchored"
            return {"below": 449_999, "threshold": 500_000, "unanchored": 450_000}[guard]
        monkeypatch.setattr(turn_context, "_preflight_request_tokens", pressure)
    try:
        assert server._start_between_turns_compaction("sid", session, result) is None
        assert session["history"] == before
        cc.compress.assert_not_called()
    finally:
        if guard == "lock": db.release_compression_lock(agent.session_id, "another")
        agent.close()
        db.close()


def test_prompt_preempts_slow_provider_without_waiting_or_late_writes(tmp_path, monkeypatch, caplog):
    from agent.auxiliary_client import _run_protected_sync_provider_call
    from agent.context_compressor import SUMMARY_PREFIX

    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    started, release, provider_done = threading.Event(), threading.Event(), threading.Event()

    def provider(kwargs):
        started.set()
        try:
            assert release.wait(10)
            return [{"role": "user", "content": SUMMARY_PREFIX + " obsolete"}]
        finally:
            provider_done.set()

    def compress(messages, **kwargs):
        return _run_protected_sync_provider_call(provider, {})

    agent.context_compressor.compress.side_effect = compress
    try:
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            job = server._start_between_turns_compaction("sid", session, {"completed": True})
            assert started.wait(5)
            assert not session["running"]
            begin = time.monotonic()
            with server._session_turn_admission(session) as admitted:
                assert admitted
                session["running"] = True
                session["history"].append({"role": "user", "content": "new prompt"})
                session["history_version"] += 1
            assert time.monotonic() - begin < 2
            assert job.done.wait(2)
            assert not release.is_set()
            assert session["history"] == before + [{"role": "user", "content": "new prompt"}]
            assert db.get_messages_as_conversation(agent.session_id) == before
            assert db.get_compression_lock_holder(agent.session_id) is None
            release.set()
            assert provider_done.wait(2)
            assert session["history_version"] == 2
            assert sum(m["content"] == "new prompt" for m in session["history"]) == 1
            assert '"trigger_source":"between_turns"' in caplog.text
            assert '"failure_class":"preempted_by_turn"' in caplog.text
            assert '"commit_status":"aborted"' in caplog.text
    finally:
        release.set()
        if started.is_set(): provider_done.wait(5)
        agent.close()
        db.close()


def test_between_turns_config_hot_reload_and_unset(tmp_path, monkeypatch):
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from agent.context_compressor import ContextCompressor

    assert DEFAULT_CONFIG["compression"]["between_turns"] is False
    cc = ContextCompressor(model="test/model", quiet_mode=True, config_context_length=1_000_000)
    agent = SimpleNamespace(model="test/model", provider="", context_compressor=cc)
    session = {"agent": agent}
    config = tmp_path / "config.yaml"
    monkeypatch.setattr(server, "_load_cfg", lambda: load_user_config_effective(config))
    config.write_text("compression:\n  between_turns: true\n  between_turns_ratio: 0.8\n")
    server._sync_agent_compression_with_config("sid", session)
    assert agent.compression_between_turns is True
    assert agent.compression_between_turns_ratio == 0.8
    config.write_text("compression:\n  between_turns: false\n  between_turns_ratio: 0.95\n")
    server._sync_agent_compression_with_config("sid", session)
    assert agent.compression_between_turns is False
    assert agent.compression_between_turns_ratio == 0.95
    config.write_text("compression: {}\n")
    server._sync_agent_compression_with_config("sid", session)
    assert agent.compression_between_turns is False
    assert agent.compression_between_turns_ratio == 0.9
