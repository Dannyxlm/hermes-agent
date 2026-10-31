"""Real bash regressions for aliased delegate terminal environment state."""

from concurrent.futures import ThreadPoolExecutor
import json
import shlex
from types import SimpleNamespace

import pytest

import tools.terminal_tool as tt
from tools.delegate_tool_child_run import _ChildRun
from tools.environments.local import LocalEnvironment


@pytest.fixture
def terminal_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setenv("TERMINAL_LOCAL_PERSISTENT", "true")
    monkeypatch.setenv("TERMINAL_TEMP_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_EXPORT_BASELINE", "baseline")
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(tt, "_container_aliases", {})
    monkeypatch.setattr(tt, "_session_cwd", {})
    monkeypatch.setattr(tt, "_check_all_guards", lambda *a, **kw: {"approved": True})
    import tools.delegate_tool_child_run as child_run
    monkeypatch.setattr(child_run, "_create_isolated_worktree", lambda *a: None)
    env = LocalEnvironment(cwd=str(tmp_path))
    container_id = tt._resolve_container_task_id("export-parent")
    tt._active_environments[container_id] = env

    def run(session, command):
        result = json.loads(tt.terminal_tool(command, task_id=session))
        assert result["exit_code"] == 0, result
        return result["output"].strip()

    def spawn(child):
        state = _ChildRun(SimpleNamespace(), SimpleNamespace(_current_task_id="export-parent"),
                          0, "test exports", child, None)
        state.seed_workspace()
        assert tt._resolve_container_task_id(child) == container_id

    yield run, spawn, tmp_path
    snapshots = list(tmp_path.glob("hermes-session-*.sh"))
    assert snapshots
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in snapshots)
    for session in ("export-parent", "export-a", "export-b"):
        tt.clear_task_env_overrides(session)
    env.cleanup()
    assert not list(tmp_path.glob("hermes-session-*.sh*"))


@pytest.mark.platforms("linux", "macos")
def test_child_exports_and_unsets_do_not_change_parent(terminal_sessions):
    run, spawn, root = terminal_sessions
    run("export-parent", "export HERMES_EXPORT_VALUE=parent")
    parent_home = run("export-parent", 'printf %s "$HOME"')
    spawn("export-a")
    child_home = root / "child-home"
    child_home.mkdir()
    run("export-a", f"export HOME={shlex.quote(str(child_home))}; unset HERMES_EXPORT_VALUE")
    assert run("export-parent", 'printf "%s:%s" "$HOME" "$HERMES_EXPORT_VALUE"') == f"{parent_home}:parent"
    assert run("export-a", 'printf "%s:%s" "$HOME" "${HERMES_EXPORT_VALUE-unset}"') == f"{child_home}:unset"


@pytest.mark.platforms("linux", "macos")
def test_parallel_children_persist_their_own_spawn_time_environment(terminal_sessions):
    run, spawn, root = terminal_sessions
    run("export-parent", "export HERMES_EXPORT_VALUE=spawn; mkdir parent-dir a-dir b-dir")
    spawn("export-a")
    spawn("export-b")
    run("export-parent", "export HERMES_EXPORT_VALUE=later; cd parent-dir")
    assert run("export-a", 'printf %s "$HERMES_EXPORT_VALUE"') == "spawn"
    assert run("export-b", 'printf %s "$HERMES_EXPORT_VALUE"') == "spawn"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, f"export-{name}",
                               f"export HERMES_EXPORT_VALUE={name}; cd {name}-dir")
                   for name in ("a", "b")]
        for future in futures:
            future.result()
    for name in ("a", "b"):
        assert run(f"export-{name}", 'printf "%s:%s" "$HERMES_EXPORT_VALUE" "$PWD"') == f"{name}:{root / (name + '-dir')}"
    assert run("export-parent", 'printf "%s:%s" "$HERMES_EXPORT_VALUE" "$PWD"') == f"later:{root / 'parent-dir'}"


@pytest.mark.platforms("linux", "macos")
def test_unset_of_launch_environment_persists_only_in_its_session(terminal_sessions):
    run, spawn, _ = terminal_sessions
    run("export-parent", "true")
    spawn("export-a")
    run("export-a", "unset HERMES_EXPORT_BASELINE")
    assert run("export-a", 'printf %s "${HERMES_EXPORT_BASELINE-unset}"') == "unset"
    assert run("export-parent", 'printf %s "$HERMES_EXPORT_BASELINE"') == "baseline"
