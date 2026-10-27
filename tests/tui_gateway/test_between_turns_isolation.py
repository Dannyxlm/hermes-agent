"""Races at the post-turn summarizer, DB commit and next-input boundaries."""
import copy
import threading
import time
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tests.tui_gateway.test_between_turns_compaction import _fixture


def test_plugin_engine_uses_its_resource_aware_clone_contract():
    from agent.between_turns_compaction import fork_compaction_agent
    from agent.context_compressor import ContextCompressor

    class Engine(ContextCompressor):
        def clone_for_agent(self):
            clone = copy.copy(self)
            clone.budget = copy.copy(self.budget)
            return clone

    engine = Engine(model="test/model", quiet_mode=True, config_context_length=1_000_000)
    engine.budget = SimpleNamespace(used=0)
    engine.opaque_resource = threading.Lock()
    class Agent:
        def __init__(self, compressor):
            self.context_compressor = compressor
    live = Agent(engine)
    shadow, _ = fork_compaction_agent(live)
    shadow.context_compressor.budget.used = 1
    assert engine.budget.used == 0
    assert shadow.context_compressor.opaque_resource is engine.opaque_resource


@pytest.mark.parametrize("publish", [False, True])
def test_speculative_anthropic_refresh_never_closes_live_client_before_commit(tmp_path, monkeypatch, publish):
    from unittest.mock import MagicMock
    from agent.between_turns_compaction import fork_compaction_agent, publish_compaction_agent, discard_compaction_agent
    from agent.conversation_compression import _refresh_main_credential
    from agent import anthropic_credentials
    from run_agent import AIAgent

    db, agent, _session, _before = _fixture(tmp_path, monkeypatch)
    old_client, new_client = MagicMock(), MagicMock()
    agent.api_mode, agent.provider = "anthropic_messages", "anthropic"
    agent._anthropic_api_key = "old-fixture"
    agent._anthropic_base_url = ""
    agent._anthropic_client = old_client
    monkeypatch.setattr(anthropic_credentials, "resolve_anthropic_token", lambda **kw: "new-fixture")
    monkeypatch.setattr(AIAgent, "_build_direct_anthropic_client", lambda *a: new_client)
    try:
        shadow, baseline = fork_compaction_agent(agent)
        _refresh_main_credential(shadow)
        old_client.close.assert_not_called()
        assert agent._anthropic_client is old_client
        assert agent._anthropic_api_key == "old-fixture"
        if publish:
            publish_compaction_agent(agent, shadow, baseline)
            assert agent._anthropic_client is new_client
            assert agent._anthropic_api_key == "new-fixture"
            old_client.close.assert_called_once()
            new_client.close.assert_not_called()
        else:
            discard_compaction_agent(shadow)
            new_client.close.assert_called_once()
            old_client.close.assert_not_called()
    finally:
        agent.close()
        db.close()


@pytest.mark.parametrize("arrival", ["admission", "steer", "drain"])
def test_uncooperative_worker_is_detached_and_cannot_write_live_state(tmp_path, monkeypatch, arrival):
    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    holder = {}
    original_compressor = agent.context_compressor
    original_prompt = original_compressor.last_prompt_tokens

    def slow_engine(messages, **kwargs):
        started.set()
        assert release.wait(10)
        holder["job"].agent.context_compressor.last_prompt_tokens = 999_999
        messages[:] = [{"role": "assistant", "content": "abandoned rewrite"}]
        return messages

    original_compressor.compress.side_effect = slow_engine
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    dispatched = []
    def dispatch(rid, sid, s, text, **kwargs):
        dispatched.append(text)
        s["running"] = True
        return True
    monkeypatch.setattr(server, "_run_prompt_submit", dispatch)
    try:
        job = server._start_between_turns_compaction("sid", session, {"completed": True})
        holder["job"] = job
        assert started.wait(5)
        begin = time.monotonic()
        if arrival == "admission":
            with server._session_turn_admission(session) as admitted:
                assert admitted
                dispatch("r", "sid", session, "next")
        elif arrival == "steer":
            assert server._session_compression_in_flight(session) is False
            dispatch("r", "sid", session, "next")
        else:
            session["queued_prompt"] = {"text": "next", "transport": None}
            assert server._drain_queued_prompt("r", "sid", session)
        assert time.monotonic() - begin < 2
        assert not job.done.is_set(), "human admission must not join an uncooperative engine"
        assert dispatched == ["next"]
        assert session["history"] == before
        assert db.get_compression_lock_holder(agent.session_id) is None
        release.set()
        assert job.done.wait(5)
        assert agent.context_compressor is original_compressor
        assert original_compressor.last_prompt_tokens == original_prompt
        assert session["history"] == before
        assert session["history_version"] == 1
        assert db.get_messages_as_conversation(agent.session_id) == before
    finally:
        release.set()
        if "job" in holder:
            holder["job"].done.wait(5)
        agent.close()
        db.close()


def test_history_edit_wins_before_commit(tmp_path, monkeypatch):
    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    summary = agent.context_compressor.compress.return_value
    def slow_engine(messages, **kwargs):
        started.set()
        assert release.wait(10)
        return summary
    agent.context_compressor.compress.side_effect = slow_engine
    try:
        job = server._start_between_turns_compaction("sid", session, {"completed": True})
        assert started.wait(5)
        with session["history_lock"]:
            session["history"] = before[:-2]
            session["history_version"] += 1
        release.set()
        assert job.done.wait(5)
        assert db.get_messages_as_conversation(agent.session_id) == before
        assert session["history"] == before[:-2]
        assert session["history_version"] == 2
    finally:
        release.set()
        agent.close()
        db.close()


def test_commit_winner_publishes_before_next_admission(tmp_path, monkeypatch):
    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    entered, release, admitted = threading.Event(), threading.Event(), threading.Event()
    original = db.archive_and_compact
    def blocked_commit(*a, **kw):
        entered.set()
        assert release.wait(10)
        return original(*a, **kw)
    monkeypatch.setattr(db, "archive_and_compact", blocked_commit)
    seen = []
    def next_turn():
        with server._session_turn_admission(session):
            seen.append((session["history_version"], len(session["history"])))
            admitted.set()
    job = server._start_between_turns_compaction("sid", session, {"completed": True})
    try:
        assert entered.wait(5)
        thread = threading.Thread(target=next_turn)
        thread.start()
        release.set()
        assert admitted.wait(5)
        thread.join(5)
        assert job.done.wait(5)
        assert seen == [(2, len(session["history"]))]
        assert len(session["history"]) < len(before)
    finally:
        release.set()
        job.done.wait(5)
        agent.close()
        db.close()


def test_background_review_state_survives_publication(tmp_path, monkeypatch):
    db, agent, session, _before = _fixture(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    summary = agent.context_compressor.compress.return_value
    agent.review_completed = 0
    def slow_engine(messages, **kwargs):
        started.set()
        assert release.wait(10)
        return summary
    agent.context_compressor.compress.side_effect = slow_engine
    try:
        job = server._start_between_turns_compaction("sid", session, {"completed": True})
        assert started.wait(5)
        agent.review_completed = 1
        release.set()
        assert job.done.wait(5)
        assert session["history_version"] == 2
        assert agent.review_completed == 1
    finally:
        release.set()
        agent.close()
        db.close()
