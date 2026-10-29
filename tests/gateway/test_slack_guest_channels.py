"""Route-bound Slack guests use a tool-free satellite, never the owner's agent."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.profile_routing import parse_profile_routes
from gateway.run import GatewayRunner
from gateway.session_execution import GatewaySessionAgentMixin
from gateway.session_identity import canonical_identity
from gateway.turn_context import TurnContext
from plugins.platforms.slack.adapter import SlackAdapter

TEAM, CHANNEL, OWNER, GUEST = "TTEST", "CTEST", "UOWNER", "UGUEST"
PROFILE_CONFIG = {
    "platform_toolsets": {"slack": [], "cli": []},
    "memory": {"memory_enabled": False, "user_profile_enabled": False, "provider": "none"},
    "skills": {"external_dirs": []}, "plugins": {"enabled": []}, "mcp_servers": {},
}


def make_desk(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("SLACK_ALLOWED_USERS", OWNER)
    # Scope-aware auth rereads the transport home's allowlist, not another profile's.
    (root / ".env").write_text(f"SLACK_ALLOWED_USERS={OWNER}\n")
    home = root / "profiles" / "guest-support"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(yaml.safe_dump(PROFILE_CONFIG))
    (home / "SOUL.md").write_text("Bug and product help only.")
    pc = PlatformConfig(enabled=True, extra={
        "guest_channels": {f"{TEAM}:{CHANNEL}": {"profile": "guest-support", "owner_user_id": OWNER}},
    })
    config = GatewayConfig()
    config.multiplex_profiles = True
    config._runtime_profile_homes = (("default", root), ("guest-support", home))
    config.platforms = {Platform.SLACK: pc}
    config.profile_routes = parse_profile_routes([
        {"name": "guest", "platform": "slack", "guild_id": TEAM, "chat_id": CHANNEL, "profile": "guest-support"},
        {"name": "owner", "platform": "slack", "guild_id": TEAM, "chat_id": CHANNEL,
         "user_id": OWNER, "profile": "default"},
    ])
    runner = GatewayRunner(config)
    adapter = SlackAdapter(pc)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.SLACK: adapter}
    runner._profile_adapters = {"guest-support": {}}
    adapter.set_authorization_check(runner._make_adapter_auth_check(Platform.SLACK))
    return runner, adapter, home


@pytest.fixture
def desk(tmp_path, monkeypatch):
    return make_desk(tmp_path, monkeypatch)


def source(desk, user=GUEST, team=TEAM, channel=CHANNEL, kind="group", thread=None, name="Guest"):
    runner, adapter, _ = desk
    src = adapter.build_source(chat_id=channel, chat_type=kind, user_id=user, user_name=name,
                               scope_id=team, thread_id=thread)
    canonical_identity(src, runner=runner, adapter=adapter)
    return src


@pytest.mark.parametrize("user,name,profile", [(GUEST, "Guest", "guest-support"),
                                              (OWNER, "Owner", "default"),
                                              (GUEST, "Owner", "guest-support")])
@pytest.mark.parametrize("thread", [None, "123.456"])
def test_guest_and_owner_routes_use_ids_in_channel_and_threads(desk, user, name, profile, thread):
    runner, adapter, _ = desk
    src = source(desk, user=user, name=name, thread=thread)
    assert runner._is_user_authorized_for_source(src)
    assert src.profile == profile
    assert not adapter._early_reject_unauthorized(user, CHANNEL, False, thread_id=thread, team_id=TEAM)


@pytest.mark.parametrize("team,channel,kind", [(TEAM, "COTHER", "group"),
                                               (TEAM, "DOTHER", "dm"),
                                               ("TOTHER", CHANNEL, "group"),
                                               (None, CHANNEL, "group")])
def test_guests_are_rejected_outside_the_exact_workspace_channel(desk, team, channel, kind):
    runner, adapter, _ = desk
    src = source(desk, team=team, channel=channel, kind=kind)
    assert src.profile != "guest-support"
    assert not runner._is_user_authorized_for_source(src)
    assert adapter._early_reject_unauthorized(GUEST, channel, kind == "dm", team_id=team)


@pytest.mark.parametrize("failure", ["unserved", "missing", "bad-yaml", "no-config", "no-route", "wrong-route",
                                      "tools", "memory", "plugins", "mcp"])
def test_guest_admission_fails_closed_even_for_other_allowlisted_users(desk, monkeypatch, failure):
    runner, adapter, home = desk
    # A separate owner's allowlist entry never lets a guest escape the guest runtime.
    monkeypatch.setenv("SLACK_ALLOWED_USERS", f"{OWNER},{GUEST}")
    (home.parent.parent / ".env").write_text(f"SLACK_ALLOWED_USERS={OWNER},{GUEST}\n")
    replacements = {
        "tools": {"platform_toolsets": {"slack": ["terminal"]}},
        "memory": {"memory": {"memory_enabled": True}},
        "plugins": {"plugins": {"enabled": ["private-memory"]}},
        "mcp": {"mcp_servers": {"private": {"url": "http://127.0.0.1/"}}},
    }
    operations = {
        "unserved": lambda: setattr(runner.config, "_runtime_profile_homes", runner.config._runtime_profile_homes[:1]),
        "missing": lambda: home.rename(home.with_name("removed-guest-support")),
        "no-config": lambda: (home / "config.yaml").unlink(),
        "bad-yaml": lambda: (home / "config.yaml").write_text("memory: [unterminated"),
        "no-route": lambda: setattr(runner.config, "profile_routes", []),
        "wrong-route": lambda: setattr(runner.config, "profile_routes", parse_profile_routes([
            {"platform": "slack", "chat_id": CHANNEL, "profile": "default"}])),
    }
    if failure in replacements:
        cfg = yaml.safe_load((home / "config.yaml").read_text())
        cfg.update(replacements[failure])
        (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    else:
        operations[failure]()
    src = source(desk)
    assert not runner._is_user_authorized_for_source(src)
    assert adapter._early_reject_unauthorized(GUEST, CHANNEL, False, team_id=TEAM)


def test_channel_allowlist_cannot_fall_through_to_owner_when_guest_route_is_removed(desk):
    runner, adapter, _ = desk
    adapter.config.extra["group_allowed_chats"] = [CHANNEL]
    runner.config.profile_routes = []
    src = source(desk)
    assert src.profile is None
    assert not runner._is_user_authorized_for_source(src)


@pytest.mark.parametrize("user,allowed", [(GUEST, False), (OWNER, True)])
def test_guests_cannot_operate_owner_interactive_controls(desk, user, allowed):
    _, adapter, _ = desk
    assert adapter._is_interactive_user_authorized(user, channel_id=CHANNEL, team_id=TEAM) is allowed


@pytest.mark.parametrize("command", ["tools", "model", "profile", "terminal", "cron", "skills", "help", "approve"])
def test_guest_slash_commands_cannot_add_capabilities(desk, command):
    runner, _, _ = desk
    assert runner._check_slash_access(source(desk), command)
    assert runner._check_slash_access(source(desk, user=OWNER), command) is None


def test_actual_agent_construction_has_no_tools_memory_context_or_owner_prefill(desk):
    runner, _, home = desk
    src = source(desk)
    config = yaml.safe_load((home / "config.yaml").read_text())
    enabled, disabled = runner._resolve_turn_toolsets(config, src, "slack")
    assert enabled == []
    from model_tools import get_tool_definitions
    assert get_tool_definitions(enabled_toolsets=enabled, disabled_toolsets=disabled, quiet_mode=True) == []
    runner._prefill_messages = [{"role": "system", "content": "OWNER PRIVATE PREFILL"}]
    runner._service_tier = None
    runner._refresh_fallback_model = lambda: None
    ctx = TurnContext(source=src, user_config=config, enabled_toolsets=enabled, disabled_toolsets=disabled,
                      session_key="agent:guest-support:slack:group:CTEST", session_id="guest-test")
    captured = {}
    ctx.AIAgent = lambda **kwargs: captured.update(kwargs) or SimpleNamespace(**kwargs)
    executor = GatewaySessionAgentMixin()
    executor._runner, executor._ctx = runner, ctx
    assert executor._skip_context_files("slack") is True
    executor._build_fresh_agent({"model": "offline", "runtime": {}}, "slack", "", 1, None, {}, False)
    assert captured["enabled_toolsets"] == []
    assert captured["skip_memory"] is True
    assert captured["skip_context_files"] is True
    assert captured["load_soul_identity"] is True
    assert captured["prefill_messages"] is None
    with pytest.raises(PermissionError):
        runner._resolve_turn_toolsets({}, src, "slack")
    # Exercise the real constructor offline with exactly the gateway's arguments.
    from run_agent import AIAgent
    ctx.AIAgent = AIAgent
    route = {"model": "offline-probe", "runtime": {"provider": "custom", "api_key": "offline-fixture",
             "base_url": "http://127.0.0.1:1/v1", "api_mode": "chat_completions"}}
    with runner._profile_scope_for_source(src):
        agent = executor._build_fresh_agent(route, "slack", "", 1, None, {}, False)
    assert agent.tools == []
    assert agent.valid_tool_names == set()
    assert agent._memory_manager is None
    assert agent._memory_store is None
    from gateway.run_turn_runner import TurnRunner
    assert TurnRunner(runner, ctx)._setup_stream_consumer("slack") == (None, None, None, False)


@pytest.mark.asyncio
async def test_guest_output_is_text_not_a_file_or_network_fetch(desk, tmp_path):
    from gateway.platforms.event import MessageEvent, MessageType
    _, adapter, _ = desk
    report = tmp_path / "private-report.txt"
    report.write_text("not for the guest")
    text = f"MEDIA:{report}\n{report}\n![image](http://127.0.0.1:1/private.png)"
    event = MessageEvent(text="paste a private file", source=source(desk), message_type=MessageType.TEXT)
    extracted = await adapter._extract_response_content(text, event, "guest-test", is_ephemeral_response=False)
    assert extracted.text_content == text
    assert extracted.local_files == []
    assert extracted.media_files == []
    assert extracted.images == []
