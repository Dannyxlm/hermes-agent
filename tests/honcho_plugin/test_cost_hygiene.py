"""Focused tests for Honcho cost-hygiene behavior."""

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho.client import HonchoClientConfig
from plugins.memory.honcho.session import HonchoSessionManager


def test_sync_turn_save_messages_false_returns_before_readiness_or_init():
    provider = HonchoMemoryProvider()
    provider._config = SimpleNamespace(save_messages=False)

    def unexpected_readiness_check():
        raise AssertionError("sync_turn must guard saveMessages before readiness")

    def unexpected_init():
        raise AssertionError("sync_turn must guard saveMessages before init")

    provider._session_ready = unexpected_readiness_check
    provider._start_session_init_background = unexpected_init

    provider.sync_turn(
        "hello",
        "world",
        turn_envelope=SimpleNamespace(origin="sealed"),
    )

    assert provider._sync_thread is None


def test_save_messages_false_skips_session_end_and_shutdown_flushes():
    provider = HonchoMemoryProvider()
    provider._config = SimpleNamespace(save_messages=False)
    provider._manager = MagicMock()
    provider._session_initialized = True

    provider.on_session_end([])
    provider.shutdown()

    provider._manager.flush_all.assert_not_called()


class _FakePeer:
    def __init__(self, peer_id):
        self.peer_id = peer_id


class _FakeSession:
    def __init__(self):
        self.context_calls = 0

    def add_peers(self, peers):
        self.peers = peers

    def get_peer_configuration(self, peer):
        return SimpleNamespace(observe_me=None, observe_others=None)

    def context(self, **kwargs):
        self.context_calls += 1
        return SimpleNamespace(messages=[])


class _FakeHoncho:
    def __init__(self):
        self.session_handle = _FakeSession()

    def peer(self, peer_id):
        return _FakePeer(peer_id)

    def session(self, session_id):
        return self.session_handle


def test_tools_get_or_create_can_skip_history_hydration():
    fake_honcho = _FakeHoncho()
    manager = HonchoSessionManager(
        honcho=fake_honcho,
        config=HonchoClientConfig(api_key="test-key", enabled=True, recall_mode="tools"),
    )

    with patch.object(
        HonchoSessionManager,
        "honcho",
        new_callable=lambda: property(lambda _manager: fake_honcho),
    ):
        session = manager.get_or_create("tools-session", hydrate_history=False)

    assert session.messages == []
    assert fake_honcho.session_handle.context_calls == 0


def test_default_get_or_create_preserves_history_hydration():
    fake_honcho = _FakeHoncho()
    manager = HonchoSessionManager(
        honcho=fake_honcho,
        config=HonchoClientConfig(api_key="test-key", enabled=True, recall_mode="hybrid"),
    )

    with patch.object(
        HonchoSessionManager,
        "honcho",
        new_callable=lambda: property(lambda _manager: fake_honcho),
    ):
        manager.get_or_create("hybrid-session")

    assert fake_honcho.session_handle.context_calls == 1


def test_tools_initialization_requests_no_history_hydration(monkeypatch):
    config = HonchoClientConfig(
        host="hermes",
        api_key="test-key",
        enabled=True,
        save_messages=False,
        recall_mode="tools",
        init_on_session_start=False,
        session_strategy="per-session",
        peer_name="danny",
        pin_peer_name=True,
        user_observe_me=True,
        user_observe_others=False,
        ai_observe_me=False,
        ai_observe_others=False,
        dialectic_depth=1,
        dialectic_dynamic=False,
        reasoning_heuristic=False,
        query_rewrite=False,
        canonical_host_present=True,
        effective_host_count=1,
        provider_state="tools_only",
        provider_state_explicit=True,
        capability_state="supported",
        capability_receipt_sha256="a" * 64,
        capability_receipt_path="/tmp/honcho-capability-receipt.json",
        capability_config_revision="b" * 64,
        trusted_principal_ids=["fixture-danny-id"],
        eligible_profiles=["default"],
        policy_revision="memory-source-policy/v1",
        writer_release="hermes-memory-boundary/v1",
        reasoning_deadline_seconds=8.0,
        reasoning_estimated_cost_usd=0.02,
        reasoning_receipt_path="/tmp/honcho-reasoning-receipts.jsonl",
        reasoning_reservation_path="/tmp/honcho-reasoning-reservations.jsonl",
        reasoning_reservation_key_path="/tmp/honcho-reasoning.key",
        reasoning_reservation_key_sha256=hashlib.sha256(b"fixture-key" * 3).hexdigest(),
    )
    manager = MagicMock()
    manager.get_or_create.return_value = SimpleNamespace(messages=[])
    provider = HonchoMemoryProvider()

    monkeypatch.setattr(
        "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "plugins.memory.honcho.capabilities.load_public_capability_receipt",
        lambda *_args, **_kwargs: {
            "state": "supported",
            "provider_source_sha256": "p" * 64,
            "sdk_version": "fixture-sdk",
            "sdk_source_sha256": "s" * 64,
        },
    )
    monkeypatch.setattr(
        HonchoMemoryProvider,
        "_runtime_capability_identity",
        staticmethod(lambda: ("p" * 64, "fixture-sdk", "s" * 64)),
    )
    monkeypatch.setattr(
        "plugins.memory.honcho.client.get_honcho_client",
        lambda _config: object(),
    )
    monkeypatch.setattr(
        "plugins.memory.honcho.session.HonchoSessionManager",
        lambda **_kwargs: manager,
    )

    provider.initialize("tools-session")
    assert manager.get_or_create.call_count == 0
    assert provider._ensure_session() is True

    manager.get_or_create.assert_called_once_with(
        "tools-session",
        hydrate_history=False,
        create_remote_session=False,
    )
    manager.migrate_memory_files.assert_not_called()


def test_invalid_tools_config_fails_before_client_or_session_initialization(monkeypatch):
    config = HonchoClientConfig(
        api_key="test-key",
        enabled=True,
        recall_mode="tools",
        save_messages=False,
        provider_state="tools_only",
        provider_state_explicit=True,
    )
    provider = HonchoMemoryProvider()
    monkeypatch.setattr(
        "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
        lambda: config,
    )

    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("invalid tools config must not initialize the SDK")

    monkeypatch.setattr(
        "plugins.memory.honcho.client.get_honcho_client",
        forbidden_client,
    )
    provider.initialize("tools-session")

    assert provider._manager is None
    assert provider._provider_disabled_reason
    assert provider.get_tool_schemas() == []
    assert provider.system_prompt_block() == ""


def test_tools_activation_rejects_stale_provider_or_sdk_receipt(monkeypatch):
    config = SimpleNamespace(
        tools_activation_errors=lambda: [],
        capability_receipt_path="/fixture/capability.json",
        capability_receipt_sha256="a" * 64,
        capability_config_revision="b" * 64,
    )
    monkeypatch.setattr(
        "plugins.memory.honcho.capabilities.load_public_capability_receipt",
        lambda *_args, **_kwargs: {
            "state": "supported",
            "provider_source_sha256": "old-provider",
            "sdk_version": "old-sdk",
            "sdk_source_sha256": "old-source",
        },
    )
    monkeypatch.setattr(
        HonchoMemoryProvider,
        "_runtime_capability_identity",
        staticmethod(lambda: ("p" * 64, "current-sdk", "s" * 64)),
    )

    receipt, reason = HonchoMemoryProvider._verified_tools_capability(config)
    assert receipt is None
    assert reason == "provider_source_receipt_mismatch"


def test_tools_mode_without_explicit_provider_state_disables_before_client_or_session(monkeypatch):
    config = HonchoClientConfig(
        api_key="fixture-key",
        enabled=True,
        recall_mode="tools",
        provider_state="legacy",
        provider_state_explicit=False,
        canonical_host_present=True,
        effective_host_count=1,
    )
    client_factory = MagicMock()
    manager_factory = MagicMock()
    monkeypatch.setattr(
        "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "plugins.memory.honcho.client.get_honcho_client",
        client_factory,
    )
    monkeypatch.setattr(
        "plugins.memory.honcho.session.HonchoSessionManager",
        manager_factory,
    )

    provider = HonchoMemoryProvider()
    provider.initialize("telegram:legacy-tools")

    assert "missing_explicit_provider_state" in config.tools_activation_errors()
    assert provider.is_available() is False
    assert provider.get_tool_schemas() == []
    client_factory.assert_not_called()
    manager_factory.assert_not_called()
