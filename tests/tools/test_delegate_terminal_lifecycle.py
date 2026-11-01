"""A timed-out delegation owns its private shell state until its worker settles."""

from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from tests.tools.test_terminal_session_exports import _in_gateway_turn
from tools import terminal_tool as tt
from tools import terminal_tool_session_env as snapshots
from tools.delegate_tool_child_run import _ChildRun
from tools.delegate_tool_routes import delegated_profile_scope

pytest_plugins = ["tests.tools.test_terminal_session_exports"]


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("profile_mode", ["launch", "inherited", "routed"])
@pytest.mark.parametrize("close_raises", [False, True])
def test_timeout_reclaims_terminal_state_only_after_worker_exit(
    terminal_sessions, monkeypatch, tmp_path, profile_mode, close_raises,
):
    from hermes_constants import get_hermes_home
    from tools import delegate_tool
    from tests.waiting import wait_until

    run, _, _ = terminal_sessions
    original_env = next(iter(tt._active_environments.values()))
    home = tmp_path / "profiles" / "worker"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("terminal:\n  env: local\n", encoding="utf-8")
    started, release, finished, closed = (threading.Event() for _ in range(4))
    seen = {}

    class Child:
        _launch_executed = True
        _delegate_profile_home = home if profile_mode == "routed" else None
        session_id = "timeout-child-session"

        def run_conversation(self, *, task_id, **kwargs):
            seen["key"] = tt._qualify_task_key(task_id)
            seen["home"] = get_hermes_home()
            run(task_id, "export HERMES_EXPORT_VALUE=child")
            seen["path"] = next(Path(entries[seen["key"]]) for entries in snapshots._snapshots.values()
                                if seen["key"] in entries)
            started.set()
            assert release.wait(timeout=30)
            assert seen["path"].exists()
            run(task_id, "export HERMES_EXPORT_VALUE=late")  # still publishes privately while unwinding
            finished.set()
            return {"final_response": "stopped", "interrupted": True}

        def hard_interrupt(self, *args):
            pass

        def get_activity_summary(self):
            return {"api_call_count": 1}

        def close(self):
            assert finished.is_set()
            seen["close_home"] = get_hermes_home()
            closed.set()
            if close_raises:
                raise RuntimeError("close failed")

    child = Child()
    state = _ChildRun(child, SimpleNamespace(_current_task_id="export-parent"),
                      0, "timeout lifecycle", "export-a", None)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 1)

    def abandon_after_start(*args):
        assert started.wait(timeout=10)

    monkeypatch.setattr(state, "wait_liveness_aware", abandon_after_start)

    def turn():
        run("export-parent", "export HERMES_EXPORT_VALUE=parent")
        state.seed_workspace()
        from tools.delegate_tool_routes import child_profile_scope
        with child_profile_scope(child):
            key = tt._qualify_task_key("export-a")
            # Routed children do not inherit shell bytes; still verify their owned seed is reclaimed.
            snapshots.seed_session_environment(key, b"declare -x HERMES_EXPORT_VALUE=child\n")
        result, failure, deferred = state.await_child()
        assert result is None and failure is not None
        assert failure["status"] == "timeout" and deferred
        state.cleanup(heartbeat=SimpleNamespace(stop=lambda: None), child_pool=None,
                      leased_cred_id=None, close_deferred=deferred)
        assert seen["path"].exists()
        assert key in snapshots._seeds
        assert "export-a" in tt._container_aliases
        assert not closed.is_set()
        return key

    try:
        if profile_mode == "inherited":
            with delegated_profile_scope(home):
                key = _in_gateway_turn("agent:worker:telegram:dm:timeout", turn)
        else:
            key = _in_gateway_turn("agent:main:telegram:dm:timeout", turn)
        # The worker's copied context has unwound by the Future's done-callback.
        release.set()
        assert closed.wait(timeout=10)
        wait_until(lambda: "export-a" not in tt._container_aliases)
        assert not seen["path"].exists()
        assert key not in snapshots._seeds
        assert all(key not in entries for entries in snapshots._snapshots.values())
        assert seen["close_home"] == seen["home"]
    finally:
        release.set()
        assert finished.wait(timeout=10)
        for env in set(tt._active_environments.values()):
            if env is not original_env:
                env.cleanup()
        with delegated_profile_scope(home):
            tt.clear_task_env_overrides("export-a")
            tt.clear_task_env_overrides("agent:worker:telegram:dm:timeout")
        run("export-parent", "true")
