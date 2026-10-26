"""Default-off and plugin compatibility at the between-turns boundary."""
from types import SimpleNamespace
import threading

from agent.context_engine import ContextEngine
from tui_gateway import server
from tests.tui_gateway.test_between_turns_compaction import _fixture


class PluginEngine(ContextEngine):
    @property
    def name(self):
        return "minimal-plugin"

    threshold_tokens = 500_000

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False, memory_context=""):
        return messages


def test_real_context_engine_without_opportunistic_api_is_skipped(tmp_path, monkeypatch):
    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    agent.context_compressor = PluginEngine()
    try:
        assert server._start_between_turns_compaction("sid", session, {"completed": True}) is None
        assert session["history"] == before
        assert "_between_turns_compaction" not in session
    finally:
        agent.close()
        db.close()


def test_default_off_manual_compress_keeps_lock_only_admission(monkeypatch):
    from hermes_cli import backend_retirement, web_server_idle_proof

    retirement = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", retirement)
    monkeypatch.setattr(web_server_idle_proof, "idle_proof", lambda: {"idle": True})
    assert retirement.prepare()["ok"]
    session = {"history_lock": threading.Lock(), "running": False}
    monkeypatch.setattr(server, "_emit_session_info_for_session", lambda *a: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: None)
    with server._manual_compress_turn("sid", session):
        assert session["running"]
        assert session["_manual_compress_active"]
    assert not session["running"]


def test_deadline_cancel_after_summary_keeps_original_commit_refusal_path():
    from agent.conversation_compression import CompressionCommitFence, _run_summary_dispatch

    fence = CompressionCommitFence()
    compressor = PluginEngine()
    agent = SimpleNamespace(context_compressor=compressor, session_id="fixture")
    messages = [{"role": "user", "content": "original"}]
    summary = [{"role": "user", "content": "summary"}]

    def compress(messages):
        fence.cancel_before_commit()  # Deadline cancellation has no explicit reason.
        return summary

    result = _run_summary_dispatch(agent, messages, compress, {}, commit_fence=fence,
                                   attempt_generation=1, hard_cancel_event=None)
    assert result is summary
    assert not fence.begin_commit()
