"""Spawn-time shell inheritance through the real ``_ChildRun.seed_workspace`` path (v0.23.2 merge).

Upstream's ``test_delegate_child_shell_state`` registers the container alias directly. CloudSeed
decides inheritance in ``seed_workspace``: a child in the parent's terminal scope starts from the
parent's exports; a profile-routed child does not copy them (and leaves no seed behind under the
parent's scope, where its own cleanup would never find it).
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from tests.tools import _child_env_fixtures

child_env = _child_env_fixtures.child_env  # fixture: real local terminal, isolated


def _sh(command: str, task_id: str) -> str:
    from agent.delegation_context import delegated_child_context
    from tools.terminal_tool import terminal_tool

    with delegated_child_context():
        result = json.loads(terminal_tool(command, task_id=task_id))
    assert result.get("exit_code", 0) == 0 and not result.get("error"), result
    return result["output"].strip()


@pytest.mark.parametrize("routed", [False, True], ids=["same-profile", "profile-routed"])
def test_seed_workspace_copies_shell_state_only_within_one_profile(child_env, tmp_path, monkeypatch, routed):
    from tools import delegate_tool_child_run as child_run
    from tools import terminal_tool as tt
    from tools import terminal_tool_session_env as snapshots
    from tools.delegate_tool_routes import child_profile_scope
    from tools.terminal_tool import terminal_tool
    from tools.terminal_tool_lifecycle import cleanup_all_environments

    monkeypatch.setattr(child_run, "_get_worktree_isolation", lambda: False)
    home = tmp_path / "profiles" / "worker"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("terminal:\n  env: local\n", encoding="utf-8")
    parent = f"parent-{uuid.uuid4().hex[:8]}"
    child = SimpleNamespace(_delegate_profile_home=home if routed else None)
    state = child_run._ChildRun(child, SimpleNamespace(_current_task_id=parent), 0, "seed",
                                f"subagent-0-{uuid.uuid4().hex[:8]}", None)
    try:
        assert json.loads(terminal_tool("export PARENT_SET=from-parent", task_id=parent)).get("exit_code", 0) == 0
        state.seed_workspace()
        with child_profile_scope(child):
            seen = _sh('printf %s "${PARENT_SET-unset}"', state.child_task_id)
        if routed:
            assert seen == "unset"
            assert tt._qualify_task_key(state.child_task_id) not in snapshots._seeds
        else:
            assert seen == "from-parent"
    finally:
        cleanup_all_environments()
        tt.clear_task_env_overrides(parent)
        with child_profile_scope(child):
            tt.clear_task_env_overrides(state.child_task_id)
