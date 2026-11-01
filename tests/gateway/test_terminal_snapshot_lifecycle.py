"""Shell exports live with a gateway conversation, not an agent-cache instance."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from tests.tools.test_terminal_session_exports import _in_gateway_turn
from tools import terminal_tool as tt
from tools import terminal_tool_session_env as snapshots
from tools.delegate_tool_routes import delegated_profile_scope

pytest_plugins = ["tests.tools.test_terminal_session_exports"]


@pytest.mark.platforms("linux", "macos")
def test_conversation_boundary_reclaims_only_owning_profiles_snapshot(terminal_sessions, tmp_path):
    run, _, _ = terminal_sessions
    original_env = next(iter(tt._active_environments.values()))
    key = "agent:worker:telegram:dm:lifecycle"
    homes = [tmp_path / name for name in ("worker", "other")]
    for home in homes:
        home.mkdir()
        (home / "config.yaml").write_text("terminal:\n  env: local\n", encoding="utf-8")
    paths, keys = [], []
    for home in homes:
        with delegated_profile_scope(home):
            _in_gateway_turn(key, lambda: run("export-parent", "export HERMES_EXPORT_VALUE=kept"))
            qualified = tt._qualify_task_key(key)
            keys.append(qualified)
            paths.append(next(Path(entries[qualified]) for entries in snapshots._snapshots.values() if qualified in entries))
            snapshots.seed_session_environment(qualified, b"declare -x HERMES_EXPORT_VALUE=seed\n")
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(_profile_home_for_key=lambda key: homes[0],
                                           set_session_yolo=lambda *args: None)
    try:
        # A boundary can run under a different request's profile, not just in the owning turn.
        with delegated_profile_scope(homes[1]):
            runner._clear_conversation_scope(key, reason="session_reset")
        assert not paths[0].exists()
        assert keys[0] not in snapshots._seeds
        assert all(keys[0] not in entries for entries in snapshots._snapshots.values())
        assert paths[1].exists() and keys[1] in snapshots._seeds
        with delegated_profile_scope(homes[0]):
            assert _in_gateway_turn(key, lambda: run("export-rotated", 'printf %s "${HERMES_EXPORT_VALUE-unset}"')) == "unset"
    finally:
        for home in homes:
            with delegated_profile_scope(home):
                tt.clear_task_env_overrides(key)
        for env in set(tt._active_environments.values()):
            if env is not original_env:
                env.cleanup()
        run("export-parent", "true")


@pytest.mark.platforms("linux", "macos")
def test_exports_survive_normal_turn_release_rotation_and_cache_rebuild(terminal_sessions, monkeypatch):
    run, _, _ = terminal_sessions
    key = "agent:main:telegram:dm:cache-rebuild"
    runner = object.__new__(GatewayRunner)
    runner._persist_active_agents = lambda: None
    runner._agent_cache_lock = None
    releases = []
    agent = SimpleNamespace(release_clients=lambda: releases.append("released"))
    runner._agent_cache = {key: agent}
    runner._running_agents = {key: agent}
    monkeypatch.setattr(runner, "_spawn_release_thread", lambda target, args, *a, **kw: target(*args))
    _in_gateway_turn(key, lambda: run("export-parent", "export HERMES_EXPORT_VALUE=kept"))
    qualified = tt._qualify_task_key(key)
    path = next(Path(entries[qualified]) for entries in snapshots._snapshots.values() if qualified in entries)
    runner._release_running_agent_state(key)
    runner._evict_cached_agent(key)
    # The memory-provider boundary evict also runs on /compress; it is NOT a shell boundary.
    runner._agent_cache[key] = SimpleNamespace(release_clients=lambda: releases.append("released"))
    runner._evict_cached_agent_at_boundary(key)
    assert releases == ["released", "released"]
    assert path.exists()
    assert _in_gateway_turn(key, lambda: run("export-rotated", 'printf %s "$HERMES_EXPORT_VALUE"')) == "kept"


@pytest.mark.asyncio
@pytest.mark.platforms("linux", "macos")
async def test_real_viewer_close_reclaims_snapshot_only_when_fifo_is_idle(terminal_sessions, tmp_path, monkeypatch):
    from tests.gateway.test_viewer_close_lifecycle import _owner, _open, _call
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import admit_session_input, cancel_session_input
    from tests.waiting import async_wait_until

    run, _, _ = terminal_sessions
    db, authority, calls = await _owner(tmp_path / "owner", monkeypatch)
    runner = object.__new__(GatewayRunner)
    runner.__dict__.update(vars(authority.runner))
    authority.runner = runner
    viewer, subscription = await _open(db, authority, tmp_path / "owner")
    route = authority.sessions["chat"].route
    _in_gateway_turn(route, lambda: run("export-parent", "export HERMES_EXPORT_VALUE=kept"))
    calls.clear()  # discard the terminal output-transform hook observed by the lifecycle spy
    qualified = tt._qualify_task_key(route)
    path = next(Path(entries[qualified]) for entries in snapshots._snapshots.values() if qualified in entries)
    row = admit_session_input(db, epoch=authority.epoch, principal_id="native", session_id="chat",
                              request_id="r1", payload={"text": "pending"})
    await _call(viewer, "session.detach", session_id="chat", subscription_id=subscription, end_reason="cli_close")
    assert path.exists() and calls == []
    cancel_session_input(db, epoch=authority.epoch, admission_id=row["admission_id"])
    authority._schedule(SessionRef(authority.profile_id, "chat"))
    await async_wait_until(lambda: len(calls) == 2)
    assert not path.exists()
    assert all(qualified not in entries for entries in snapshots._snapshots.values())
    run("export-parent", "true")
