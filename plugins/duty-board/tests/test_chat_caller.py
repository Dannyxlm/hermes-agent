"""Caller resolution against a real session-context module.

The other chat tests stub ``commands._session``. These do not: they bind the
session ContextVars (or leave them unset) the way Hermes does and let the
command resolve the caller itself, so a regression there shows up here.

They use Hermes's own ``gateway.session_context`` when it is importable, and a
faithful stand-in otherwise (``tests/fixtures/fake_session_context.py``).
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest

from .fixtures.make_home import add_message, add_session

OWNER = "222222222222222222"


@pytest.fixture(params=["host", "stand-in"])
def session_context(request, monkeypatch):
    if request.param == "host":
        try:
            module = importlib.import_module("gateway.session_context")
        except ImportError:
            pytest.skip("Hermes is not importable here")
    else:
        from .fixtures import fake_session_context as module

        gateway = type(sys)("gateway")
        gateway.session_context = module
        monkeypatch.setitem(sys.modules, "gateway", gateway)
        monkeypatch.setitem(sys.modules, "gateway.session_context", module)
    module.reset_session_vars()
    yield module
    module.reset_session_vars()


@pytest.fixture
def duty(commands, session_context, monkeypatch):
    # Undo conftest's stubs so the command resolves the caller and the process itself.
    monkeypatch.setattr(commands, "_session", commands.real_resolvers["_session"])
    monkeypatch.setattr(commands, "_in_gateway", commands.real_resolvers["_in_gateway"])
    return commands.duty


def _as_gateway_process(monkeypatch, is_gateway=True):
    """Make this test process look like the live gateway: the marker is set
    and the gateway PID file names this process."""
    import os

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    status = type(sys)("gateway.status")
    status.get_running_pid = lambda cleanup_stale=True: os.getpid() if is_gateway else os.getpid() + 1
    monkeypatch.setitem(sys.modules, "gateway.status", status)
    package = sys.modules.get("gateway")
    if package is not None:
        monkeypatch.setattr(package, "status", status, raising=False)


def _settings(home, **values):
    (home / "duty-board" / "settings.json").write_text(json.dumps(values), encoding="utf-8")


def _waiting(home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "mine", title="OWNERTITLE", now=now)
    add_message(home, "mine", "assistant", "May I continue?", timestamp=now - 300)


def test_process_environment_never_identifies_the_caller(duty, home, monkeypatch):
    """A local CLI/TUI started from a gateway-spawned terminal inherits the
    gateway's HERMES_SESSION_* values in os.environ. They describe whoever ran
    that earlier turn, not the person typing now."""
    _settings(home, owner=OWNER)
    _waiting(home)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", OWNER)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    _as_gateway_process(monkeypatch)
    # Inside a gateway nothing is bound for this call: an old host. It must refuse
    # even though os.environ names the owner in a DM.
    result = duty()
    assert "OWNERTITLE" not in result
    assert "0.21.7" in result


def test_inherited_environment_does_not_lock_out_the_local_cli(duty, home, monkeypatch):
    _settings(home, owner=OWNER)
    _waiting(home)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "999")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "channel")
    # The hermes CLI imports gateway.run, which sets the marker, but it is not the gateway.
    _as_gateway_process(monkeypatch, is_gateway=False)
    result = duty()
    assert "only available" not in result
    assert "OWNERTITLE" in result


def test_bound_owner_and_stranger(duty, home, monkeypatch, session_context):
    _settings(home, owner=OWNER)
    _waiting(home)
    _as_gateway_process(monkeypatch)
    tokens = session_context.set_session_vars(platform="discord", user_id=OWNER, chat_type="dm", chat_id="1")
    try:
        assert "OWNERTITLE" in duty()
    finally:
        session_context.clear_session_vars(tokens)
    session_context.reset_session_vars()
    tokens = session_context.set_session_vars(platform="discord", user_id="999", chat_type="dm", chat_id="1")
    try:
        assert "only available" in duty()
    finally:
        session_context.clear_session_vars(tokens)


def test_bound_thread_gets_counts(duty, home, monkeypatch, session_context):
    _settings(home, owner=OWNER)
    _waiting(home)
    _as_gateway_process(monkeypatch)
    tokens = session_context.set_session_vars(platform="discord", user_id=OWNER, chat_type="thread", chat_id="1", thread_id="2")
    try:
        result = duty()
    finally:
        session_context.clear_session_vars(tokens)
    assert "OWNERTITLE" not in result
    assert "1 waiting on you" in result


def test_bound_messaging_surface_without_a_caller_fails_closed(duty, home, session_context):
    """A surface bound with a messaging platform but no user id must not fall
    through to local trust, even outside a gateway process."""
    _settings(home, owner=OWNER)
    _waiting(home)
    tokens = session_context.set_session_vars(platform="telegram", chat_type="dm", chat_id="1")
    try:
        result = duty()
    finally:
        session_context.clear_session_vars(tokens)
    assert "OWNERTITLE" not in result
    assert "only available" in result


def test_cleared_context_is_local(duty, home, session_context):
    """After a turn the host clears the vars to "" (not unset). That is a bound,
    empty, local context: the local user is trusted."""
    _settings(home, owner=OWNER)
    _waiting(home)
    tokens = session_context.set_session_vars(platform="discord", user_id=OWNER, chat_type="dm", chat_id="1")
    session_context.clear_session_vars(tokens)
    assert "OWNERTITLE" in duty()


@pytest.mark.parametrize("platform,chat_id,private", [
    ("slack", "D0123", True),        # one-to-one IM
    ("slack", "G0456", False),       # group DM (mpim): the host reports chat_type "dm"
    ("slack", "C0789", False),
    ("discord", "1234", True),       # Discord DMs: the host only says "dm" for DMChannel or no guild
    ("telegram", "1234", True),
    ("matrix", "!room:x", False),    # m.direct rooms can hold more than two people
])
def test_dm_means_private_only_where_the_host_proves_it(duty, home, monkeypatch, session_context, platform, chat_id, private):
    """Some adapters label a shared conversation as "dm" (Slack groups im and
    mpim together). Full details need a destination that is provably one-to-one."""
    _settings(home, owner=OWNER)
    _waiting(home)
    _as_gateway_process(monkeypatch)
    tokens = session_context.set_session_vars(platform=platform, user_id=OWNER, chat_type="dm", chat_id=chat_id)
    try:
        result = duty()
    finally:
        session_context.clear_session_vars(tokens)
    assert ("OWNERTITLE" in result) is private
    assert "1 waiting on you" in result


def test_routed_profile_decides_owner_and_data(duty, home, tmp_path, monkeypatch, session_context):
    """In a multi-profile gateway the host routes a chat to another profile
    with a context-local home override; HERMES_HOME in the environment still
    names the launch profile. The routed profile's owner and data apply."""
    from .fixtures.make_home import make_home

    _settings(home, owner=OWNER)
    _waiting(home)
    routed = make_home(tmp_path / "routed-profile")
    (routed / "duty-board" / "settings.json").write_text(json.dumps({"owner": "999"}), encoding="utf-8")
    commands = sys.modules[duty.__module__]
    monkeypatch.setattr(commands.board, "get_hermes_home", lambda: routed)
    _as_gateway_process(monkeypatch)
    tokens = session_context.set_session_vars(platform="discord", user_id=OWNER, chat_type="dm", chat_id="1")
    try:
        result = duty()
    finally:
        session_context.clear_session_vars(tokens)
    assert "OWNERTITLE" not in result
    assert "only available" in result


@pytest.mark.parametrize("chat_id", ["!room:example.org", "!dm:example.org"])
def test_matrix_dm_label_is_not_trusted(duty, home, monkeypatch, session_context, chat_id):
    """Matrix calls a room "dm" from m.direct account data plus a missing room
    name, even with three or more members. Nothing bound proves one-to-one."""
    _settings(home, owner=OWNER)
    _waiting(home)
    _as_gateway_process(monkeypatch)
    tokens = session_context.set_session_vars(platform="matrix", user_id=OWNER, chat_type="dm", chat_id=chat_id)
    try:
        result = duty()
    finally:
        session_context.clear_session_vars(tokens)
    assert "OWNERTITLE" not in result
    assert "1 waiting on you" in result


@pytest.mark.parametrize("platform", ["telegram", "discord", "feishu", "weixin", "unknownplatform"])
def test_dm_details_only_on_platforms_with_a_proven_one_to_one_label(duty, home, monkeypatch, session_context, platform):
    _settings(home, owner=OWNER)
    _waiting(home)
    _as_gateway_process(monkeypatch)
    tokens = session_context.set_session_vars(platform=platform, user_id=OWNER, chat_type="dm", chat_id="1")
    try:
        result = duty()
    finally:
        session_context.clear_session_vars(tokens)
    assert ("OWNERTITLE" in result) is (platform in {"telegram", "discord", "feishu"})


def test_plugin_host_child_refuses_chat_and_keeps_data(duty, home, monkeypatch):
    """With plugins.isolation: host the command runs in a child process that
    receives the arguments but none of the caller's session. It cannot tell
    the owner from anyone else, or a DM from a channel, so it refuses."""
    _settings(home, owner=OWNER)
    _waiting(home)
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    result = duty()
    assert "OWNERTITLE" not in result
    assert "plugins.isolation" in result
    # The host skips register_cli_command in the child too, so `hermes duty` does
    # not exist there: the refusal must not send the user to it.
    assert "hermes duty" not in result


def test_isolation_refusal_never_points_to_the_cli_in_any_locale():
    import glob
    for path in sorted(glob.glob(str(Path(__file__).resolve().parents[1] / "dashboard/i18n/*.json"))):
        text = json.load(open(path, encoding="utf-8-sig"))["chat"]["isolated"]
        assert "hermes duty" not in text, path
        assert "plugins.isolation: host" in text, path


def test_routed_profile_ignores_the_launch_profile_owner_variable(duty, home, tmp_path, monkeypatch, session_context):
    """DUTY_BOARD_OWNER in the launch environment belongs to the launch profile.
    A routed profile's own scoped value, or its settings, decide its owner."""
    from .fixtures.make_home import make_home

    _settings(home, owner=OWNER)
    routed = make_home(tmp_path / "routed-owner")
    (routed / "duty-board" / "settings.json").write_text(json.dumps({"owner": "999"}), encoding="utf-8")
    now = float((routed / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(routed, "routed", title="ROUTEDTITLE", now=now)
    add_message(routed, "routed", "assistant", "May I continue?", timestamp=now - 300)
    commands = sys.modules[duty.__module__]
    monkeypatch.setattr(commands.board, "get_hermes_home", lambda: routed)
    monkeypatch.setenv("DUTY_BOARD_OWNER", OWNER)
    # The host's profile-scoped secret lookup: the routed profile has no
    # DUTY_BOARD_OWNER of its own, so the scoped answer is "unset".
    monkeypatch.setattr(commands.board, "get_secret", lambda name, default=None: default)
    _as_gateway_process(monkeypatch)
    tokens = session_context.set_session_vars(platform="discord", user_id=OWNER, chat_type="dm", chat_id="1")
    try:
        result = duty()
    finally:
        session_context.clear_session_vars(tokens)
    assert "ROUTEDTITLE" not in result
    assert "only available" in result
