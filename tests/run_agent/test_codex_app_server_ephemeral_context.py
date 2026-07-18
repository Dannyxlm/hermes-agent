"""Regression coverage for ephemeral context on the Codex app-server path."""

from unittest.mock import MagicMock, patch

from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult
from tests.run_agent.test_codex_app_server_integration import _make_codex_agent


def _turn(final_text: str = "done") -> TurnResult:
    return TurnResult(
        final_text=final_text,
        projected_messages=[{"role": "assistant", "content": final_text}],
        turn_id="turn-context",
        thread_id="thread-context",
    )


def _patch_session(monkeypatch, seen_inputs):
    def fake_run_turn(self, user_input: str, **kwargs):
        seen_inputs.append(user_input)
        return _turn()

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(CodexAppServerSession, "ensure_started", lambda self: "thread-context")


def test_codex_composes_external_memory_before_plugin_context(monkeypatch):
    seen_inputs = []
    _patch_session(monkeypatch, seen_inputs)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook, **kwargs: [{"context": "PLUGIN_CONTEXT"}] if hook == "pre_llm_call" else [],
    )
    agent = _make_codex_agent()
    agent._memory_manager = MagicMock()
    agent._memory_manager.build_system_prompt.return_value = ""
    agent._memory_manager.prefetch_all.return_value = "MEMORY_CONTEXT"

    with patch.object(agent, "_spawn_background_review", return_value=None):
        agent.run_conversation("ORIGINAL_USER_TEXT")

    assert len(seen_inputs) == 1
    sent = seen_inputs[0]
    assert sent.startswith("ORIGINAL_USER_TEXT\n\n<memory-context>")
    assert sent.index("MEMORY_CONTEXT") < sent.index("PLUGIN_CONTEXT")
    assert "MEMORY_CONTEXT" not in (agent._cached_system_prompt or "")
    assert "PLUGIN_CONTEXT" not in (agent._cached_system_prompt or "")


def test_codex_empty_context_sends_original_text(monkeypatch):
    seen_inputs = []
    _patch_session(monkeypatch, seen_inputs)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda hook, **kwargs: [])
    agent = _make_codex_agent()

    with patch.object(agent, "_spawn_background_review", return_value=None):
        agent.run_conversation("ORIGINAL_ONLY")

    assert seen_inputs == ["ORIGINAL_ONLY"]


def test_codex_ephemeral_context_does_not_persist_in_transcript(monkeypatch):
    seen_inputs = []
    _patch_session(monkeypatch, seen_inputs)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook, **kwargs: ["PLUGIN_CONTEXT"] if hook == "pre_llm_call" else [],
    )
    agent = _make_codex_agent()
    agent._memory_manager = MagicMock()
    agent._memory_manager.build_system_prompt.return_value = ""
    agent._memory_manager.prefetch_all.return_value = "MEMORY_CONTEXT"

    with patch.object(agent, "_spawn_background_review", return_value=None):
        result = agent.run_conversation("TRANSCRIPT_USER_TEXT")

    user_messages = [message for message in result["messages"] if message.get("role") == "user"]
    assert user_messages == [{"role": "user", "content": "TRANSCRIPT_USER_TEXT"}]
    assert "MEMORY_CONTEXT" in seen_inputs[0]
    assert "PLUGIN_CONTEXT" in seen_inputs[0]


def test_codex_ephemeral_context_is_appended_exactly_once(monkeypatch):
    seen_inputs = []
    _patch_session(monkeypatch, seen_inputs)
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook, **kwargs: ["UNIQUE_PLUGIN_CONTEXT"] if hook == "pre_llm_call" else [],
    )
    agent = _make_codex_agent()
    agent._memory_manager = MagicMock()
    agent._memory_manager.build_system_prompt.return_value = ""
    agent._memory_manager.prefetch_all.return_value = "UNIQUE_MEMORY_CONTEXT"

    with patch.object(agent, "_spawn_background_review", return_value=None):
        result = agent.run_conversation("UNIQUE_USER_TEXT")

    assert len(seen_inputs) == 1
    assert seen_inputs[0].count("UNIQUE_MEMORY_CONTEXT") == 1
    assert seen_inputs[0].count("UNIQUE_PLUGIN_CONTEXT") == 1
    assert sum(message.get("role") == "user" for message in result["messages"]) == 1
