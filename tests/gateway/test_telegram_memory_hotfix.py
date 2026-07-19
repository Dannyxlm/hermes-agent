"""Behavior coverage for the frozen Telegram memory hotfix."""

import asyncio
import sys
import threading
import types
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="owner-1",
        chat_id="chat-1",
        user_name="owner",
        chat_type="dm",
        message_id="message-1",
    )


def _event(*, internal: bool) -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="message-1",
        internal=internal,
    )


def _session_entry() -> SessionEntry:
    created_at = datetime.now()
    return SessionEntry(
        session_key=build_session_key(_source()),
        session_id="session-1",
        created_at=created_at,
        updated_at=created_at + timedelta(seconds=1),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _local_agent_result():
    return {
        "final_response": "local reply",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "local reply"},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "api_calls": 1,
        "failed": False,
    }


async def _wait_for_degraded_receipts(runner, count: int):
    for _ in range(100):
        receipts = getattr(runner, "_memory_degraded_receipts", [])
        if len(receipts) >= count:
            return receipts
        await asyncio.sleep(0.005)
    return getattr(runner, "_memory_degraded_receipts", [])


def _runner(monkeypatch, tmp_path):
    runner = gateway_run.GatewayRunner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="test-token")
            }
        )
    )

    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter.send_typing = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}

    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry()
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._session_db = None

    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._update_prompt_pending = {}
    runner._external_drain_active = False
    runner._startup_restore_in_progress = False
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()

    runner._is_user_authorized = lambda _source: True
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._claim_active_session_slot = lambda _key, _source: (None, None)
    runner._persist_active_agents = lambda: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    runner._is_session_run_current = lambda _key, _generation: True
    runner._bind_adapter_run_generation = lambda *_args: None
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._deliver_platform_notice = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=lambda **kwargs: kwargs["event"].text
    )
    runner._post_turn_goal_continuation = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._run_agent = AsyncMock(return_value=_local_agent_result())

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: []
    )
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_real_telegram_outer_handler_passes_event_origin_to_inner_handler(
    monkeypatch, tmp_path, internal
):
    runner = _runner(monkeypatch, tmp_path)
    issued_for = []
    real_issue = gateway_run._issue_post_auth_memory_origin

    def recording_issue(source, *, internal):
        issued_for.append(internal)
        return real_issue(source, internal=internal)

    monkeypatch.setattr(gateway_run, "_issue_post_auth_memory_origin", recording_issue)

    response = await runner._handle_message(_event(internal=internal))

    assert response == "local reply"
    assert issued_for == [internal]
    runner._run_agent.assert_awaited_once()
    assert (
        runner._run_agent.await_args.kwargs["external_memory_disabled"] is False
    )


@pytest.mark.asyncio
async def test_optional_memory_setup_failure_forces_transient_local_only_turn(
    monkeypatch, tmp_path
):
    runner = _runner(monkeypatch, tmp_path)

    def fail_optional_memory_setup(_source, *, internal):
        raise RuntimeError("origin unavailable")

    monkeypatch.setattr(
        gateway_run,
        "_issue_post_auth_memory_origin",
        fail_optional_memory_setup,
    )

    response = await asyncio.wait_for(
        runner._handle_message(_event(internal=False)), timeout=1.0
    )

    assert response == "local reply"
    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["external_memory_disabled"] is True
    receipts = await _wait_for_degraded_receipts(runner, 1)
    receipt = receipts[-1]
    assert receipt["type"] == "memory_degraded_deny"
    assert receipt["reason_code"] == "origin_unavailable"
    assert receipt["external_memory_disabled"] is True


@pytest.mark.asyncio
async def test_actual_deny_factory_failure_cannot_block_reply(monkeypatch, tmp_path):
    import agent.memory_provenance as memory_provenance

    runner = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_issue_post_auth_memory_origin",
        MagicMock(side_effect=RuntimeError("origin unavailable")),
    )
    monkeypatch.setattr(
        memory_provenance,
        "issue_deny_origin",
        MagicMock(side_effect=RuntimeError("deny unavailable")),
    )

    response = await asyncio.wait_for(
        runner._handle_message(_event(internal=False)), timeout=1.0
    )

    assert response == "local reply"
    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["external_memory_disabled"] is True
    receipts = await _wait_for_degraded_receipts(runner, 2)
    assert {r["reason_code"] for r in receipts} == {
        "origin_unavailable",
        "deny_binding_unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "failure"),
    [
        ("_store_memory_degraded_receipt", OSError("receipt storage failed")),
        ("_increment_memory_degraded_metric", RuntimeError("metric failed")),
        ("_publish_memory_degraded_receipt", TypeError("serialization failed")),
        (
            "_publish_memory_degraded_receipt",
            TimeoutError("event publication timed out"),
        ),
    ],
    ids=["receipt", "metric", "serialization", "event-timeout"],
)
async def test_degraded_reporting_boundaries_cannot_block_reply(
    monkeypatch, tmp_path, boundary, failure
):
    runner = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_issue_post_auth_memory_origin",
        MagicMock(side_effect=RuntimeError("origin unavailable")),
    )
    monkeypatch.setattr(runner, boundary, MagicMock(side_effect=failure))

    response = await asyncio.wait_for(
        runner._handle_message(_event(internal=False)), timeout=1.0
    )

    assert response == "local reply"
    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["external_memory_disabled"] is True


@pytest.mark.asyncio
async def test_blocking_degraded_publisher_cannot_block_reply(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_issue_post_auth_memory_origin",
        MagicMock(side_effect=RuntimeError("origin unavailable")),
    )
    publisher_started = threading.Event()
    release_publisher = threading.Event()

    def block_publication(_receipt):
        publisher_started.set()
        release_publisher.wait(timeout=5.0)

    monkeypatch.setattr(runner, "_publish_memory_degraded_receipt", block_publication)

    try:
        response = await asyncio.wait_for(
            runner._handle_message(_event(internal=False)), timeout=0.5
        )
        assert response == "local reply"
        assert publisher_started.wait(timeout=0.5)
        runner._run_agent.assert_awaited_once()
    finally:
        release_publisher.set()


@pytest.mark.asyncio
async def test_optional_memory_deny_binding_and_failure_logging_are_non_fatal(
    monkeypatch, tmp_path
):
    runner = _runner(monkeypatch, tmp_path)
    runner._set_session_env = MagicMock(
        side_effect=RuntimeError("denial receipt binding failed")
    )
    monkeypatch.setattr(
        gateway_run.logger,
        "warning",
        MagicMock(side_effect=RuntimeError("degraded metric logger failed")),
    )

    response = await asyncio.wait_for(
        runner._handle_message(_event(internal=False)), timeout=1.0
    )

    assert response == "local reply"
    runner._run_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_degraded_turn_bypasses_healthy_cache_and_skips_provider_init(
    monkeypatch, tmp_path
):
    from tests.gateway.test_run_progress_interrupt import (
        ProgressCaptureAdapter,
        _make_runner as _make_runtime_runner,
    )

    created_kwargs = []
    provider_calls = []
    released = threading.Event()

    class DegradedAgent:
        def __init__(self, **kwargs):
            created_kwargs.append(kwargs)
            if not kwargs.get("skip_memory"):
                provider_calls.append("initialize")
            self.tools = []
            # A transient agent may activate fallback. It still must not evict
            # the unrelated healthy cache entry it deliberately bypassed.
            self.model = "unexpected-fallback"
            self._interrupt_requested = False

        @property
        def is_interrupted(self):
            return self._interrupt_requested

        def run_conversation(self, _message, **_kwargs):
            return _local_agent_result()

        def release_clients(self):
            released.set()

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = DegradedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )

    adapter = ProgressCaptureAdapter()
    runner = _make_runtime_runner(adapter)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    session_key = "agent:main:telegram:dm:chat-1"
    healthy_cached_agent = object()
    runner._agent_cache[session_key] = (
        healthy_cached_agent,
        "healthy-signature",
        None,
        "session-1",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key=session_key,
        external_memory_disabled=True,
    )

    assert result["final_response"] == "local reply"
    assert provider_calls == []
    assert created_kwargs[-1]["skip_memory"] is True
    assert runner._agent_cache[session_key][0] is healthy_cached_agent
    assert released.wait(timeout=1.0)


@pytest.mark.asyncio
async def test_envelope_preflight_failure_skips_provider_initialization(
    monkeypatch, tmp_path
):
    from tests.gateway.test_run_progress_interrupt import (
        ProgressCaptureAdapter,
        _make_runner as _make_runtime_runner,
    )

    created_kwargs = []
    provider_calls = []

    class PreflightDegradedAgent:
        def __init__(self, **kwargs):
            created_kwargs.append(kwargs)
            if not kwargs.get("skip_memory"):
                provider_calls.append("initialize")
            self.tools = []
            self.model = kwargs.get("model", "test/model")
            self._interrupt_requested = False

        @property
        def is_interrupted(self):
            return self._interrupt_requested

        def run_conversation(self, _message, **_kwargs):
            return _local_agent_result()

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = PreflightDegradedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )
    envelope_factory = MagicMock(
        side_effect=RuntimeError("envelope factory unavailable")
    )
    monkeypatch.setattr(
        "agent.memory_provenance.issue_turn_envelope",
        envelope_factory,
    )

    adapter = ProgressCaptureAdapter()
    runner = _make_runtime_runner(adapter)
    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:preflight",
    )

    assert result["final_response"] == "local reply"
    assert provider_calls == []
    assert created_kwargs[-1]["skip_memory"] is True
    envelope_factory.assert_called_once()


@pytest.mark.asyncio
async def test_degraded_proxy_turn_is_not_sent_remote_or_run_locally(
    monkeypatch, tmp_path
):
    from tests.gateway.test_run_progress_interrupt import (
        ProgressCaptureAdapter,
        _make_runner as _make_runtime_runner,
    )

    adapter = ProgressCaptureAdapter()
    runner = _make_runtime_runner(adapter)
    runner._run_agent_via_proxy = AsyncMock(
        side_effect=AssertionError("degraded content must not reach proxy")
    )
    monkeypatch.setattr(runner, "_get_proxy_url", lambda: "https://proxy.invalid")

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:chat-1",
        external_memory_disabled=True,
    )

    assert result["local_only_degraded"] is True
    assert result["api_calls"] == 0
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["error"] == "memory_provenance_unavailable"
    assert result["agent_persisted"] is False
    assert result["suppress_transcript_persistence"] is True
    assert "did not send it to the remote agent" in result["final_response"]
    runner._run_agent_via_proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_proxy_message_never_enters_later_healthy_history(
    monkeypatch, tmp_path
):
    runner = _runner(monkeypatch, tmp_path)
    denied_text = "must remain on this gateway"
    degraded = {
        "final_response": "safe retry",
        "messages": [],
        "api_calls": 0,
        "tools": [],
        "history_offset": 0,
        "failed": True,
        "completed": False,
        "error": "memory_provenance_unavailable",
        "agent_persisted": False,
        "suppress_transcript_persistence": True,
    }
    runner._run_agent = AsyncMock(side_effect=[degraded, _local_agent_result()])

    first_event = _event(internal=False)
    first_event.text = denied_text
    first_response = await runner._handle_message(first_event)

    assert first_response == "safe retry"
    runner.session_store.append_to_transcript.assert_not_called()

    second_response = await runner._handle_message(_event(internal=False))

    assert second_response == "local reply"
    second_history = runner._run_agent.await_args_list[1].kwargs["history"]
    assert second_history == []
    assert denied_text not in str(second_history)


@pytest.mark.asyncio
async def test_late_degraded_agent_is_evicted_when_turn_raises(monkeypatch, tmp_path):
    from tests.gateway.test_run_progress_interrupt import (
        ProgressCaptureAdapter,
        _make_runner as _make_runtime_runner,
    )

    released = threading.Event()

    class LateFailureAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self.model = kwargs.get("model", "test/model")
            self._interrupt_requested = False

        @property
        def is_interrupted(self):
            return self._interrupt_requested

        def run_conversation(self, _message, **_kwargs):
            self._evict_after_memory_degraded = True
            raise RuntimeError("late turn failure")

        def release_clients(self):
            released.set()

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = LateFailureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )

    adapter = ProgressCaptureAdapter()
    runner = _make_runtime_runner(adapter)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    session_key = "agent:main:telegram:dm:late-failure"

    with pytest.raises(RuntimeError, match="late turn failure"):
        await runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=_source(),
            session_id="session-1",
            session_key=session_key,
        )

    assert session_key not in runner._agent_cache
    assert released.wait(timeout=1.0)


def test_turn_envelope_failure_disables_provider_before_prompt_and_model(
    monkeypatch
):
    from tests.agent.test_turn_context import _FakeAgent, _build

    agent = _FakeAgent()
    manager = MagicMock()
    manager.get_all_tool_names.return_value = {"memory_profile", "memory_search"}
    agent._memory_manager = manager
    agent.tools = [
        {"type": "function", "function": {"name": "memory_profile"}},
        {"type": "function", "function": {"name": "safe_local_tool"}},
    ]
    agent.valid_tool_names = {"memory_profile", "memory_search", "safe_local_tool"}
    degraded = MagicMock()
    agent._memory_degraded_callback = degraded
    agent._build_system_prompt = MagicMock(return_value="LOCAL-ONLY SYSTEM")

    monkeypatch.setattr(
        "agent.memory_provenance.issue_turn_envelope",
        MagicMock(side_effect=RuntimeError("envelope unavailable")),
    )
    monkeypatch.setattr("agent.auxiliary_client.set_runtime_main", lambda *_a, **_k: None)

    ctx = _build(agent)

    assert ctx.memory_turn_envelope is None
    assert ctx.active_system_prompt == "LOCAL-ONLY SYSTEM"
    assert agent._memory_manager is None
    assert agent._external_memory_disabled is True
    assert agent._evict_after_memory_degraded is True
    assert {tool["function"]["name"] for tool in agent.tools} == {"safe_local_tool"}
    assert agent.valid_tool_names == {"safe_local_tool"}
    manager.on_turn_start.assert_not_called()
    manager.prefetch_all.assert_not_called()
    degraded.assert_called_once_with("turn_envelope_unavailable")


def test_pre_disabled_turn_skips_turn_envelope_factory(monkeypatch):
    from tests.agent.test_turn_context import _FakeAgent, _build

    agent = _FakeAgent()
    agent._external_memory_disabled = True
    agent._cached_system_prompt = None
    agent._build_system_prompt = MagicMock(return_value="LOCAL-ONLY SYSTEM")
    envelope_factory = MagicMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(
        "agent.memory_provenance.issue_turn_envelope",
        envelope_factory,
    )
    monkeypatch.setattr("agent.auxiliary_client.set_runtime_main", lambda *_a, **_k: None)

    ctx = _build(agent)

    assert ctx.memory_turn_envelope is None
    assert ctx.active_system_prompt == "LOCAL-ONLY SYSTEM"
    envelope_factory.assert_not_called()


def test_turn_context_reuses_preissued_envelope_without_second_factory_call(
    monkeypatch
):
    from agent.memory_provenance import issue_deny_origin, issue_turn_envelope
    from tests.agent.test_turn_context import _FakeAgent, _build

    agent = _FakeAgent()
    origin = issue_deny_origin(
        runtime_class="gateway",
        platform="telegram",
    )
    envelope = issue_turn_envelope(
        origin,
        session_id=agent.session_id,
        turn_id="preissued-turn",
        message_id="message-1",
        user_content="hello",
    )
    agent._preissued_memory_turn_envelope = envelope
    agent._preissued_memory_turn_id = "preissued-turn"
    second_factory = MagicMock(side_effect=AssertionError("must not mint twice"))
    monkeypatch.setattr(
        "agent.memory_provenance.issue_turn_envelope",
        second_factory,
    )
    monkeypatch.setattr("agent.auxiliary_client.set_runtime_main", lambda *_a, **_k: None)

    ctx = _build(agent)

    assert ctx.memory_turn_envelope is envelope
    assert agent._current_turn_id == "preissued-turn"
    second_factory.assert_not_called()


def test_degraded_continuation_never_restores_stored_provider_prompt(monkeypatch):
    from tests.agent.test_turn_context import _FakeAgent, _build

    agent = _FakeAgent()
    agent._external_memory_disabled = True
    agent._cached_system_prompt = None
    agent._memory_manager = None
    agent._build_system_prompt = MagicMock(return_value="LOCAL-ONLY SYSTEM")
    agent._session_db = MagicMock()
    agent._session_db.get_session.return_value = {
        "system_prompt": "EXTERNAL MEMORY PROVIDER USER IDENTIFIER"
    }
    monkeypatch.setattr("agent.auxiliary_client.set_runtime_main", lambda *_a, **_k: None)

    ctx = _build(
        agent,
        conversation_history=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "reply"},
        ],
    )

    assert ctx.active_system_prompt == "LOCAL-ONLY SYSTEM"
    assert "EXTERNAL MEMORY" not in ctx.active_system_prompt
    agent._session_db.get_session.assert_not_called()
    assert agent._cached_system_prompt == "LOCAL-ONLY SYSTEM"


def test_degraded_prompt_is_suppressed_across_session_create_retry(monkeypatch):
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._persist_disabled = False
    agent._session_db_created = False
    agent._session_db = MagicMock()
    agent._session_db.create_session.side_effect = [
        RuntimeError("database is locked"),
        None,
    ]
    agent.platform = "telegram"
    agent.session_id = "session-1"
    agent.model = "test/model"
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "LOCAL-ONLY SYSTEM"
    agent._parent_session_id = None
    agent._suppress_system_prompt_persistence = True
    monkeypatch.setattr("run_agent._launch_cwd_for_session", lambda _source: None)

    agent._ensure_db_session()
    assert agent._session_db_created is False
    agent._ensure_db_session()

    assert agent._session_db_created is True
    assert agent._session_db.create_session.call_count == 2
    for call in agent._session_db.create_session.call_args_list:
        assert call.kwargs["system_prompt"] is None
