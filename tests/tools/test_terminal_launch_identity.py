"""Terminal-only launch attribution is task-local and never enters shared snapshots."""
import os
import subprocess

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools.environments.local import (
    _make_run_env, _sanitize_subprocess_env, build_subprocess_env, hermes_subprocess_env,
)
from tools.environments.base_session_env import _export_dump_excluding_session_vars
from tools.process_registry import ProcessRegistry


@pytest.mark.parametrize("builder", [_make_run_env, ProcessRegistry._spawn_env])
def test_terminal_launcher_uses_bound_identity(monkeypatch, builder):
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "foreign")
    tokens = set_session_vars(session_id="launcher", profile="atlas")
    try:
        env = builder({})
        assert env["HERMES_STARTED_BY_SESSION_ID"] == "launcher"
        assert env["HERMES_STARTED_BY_PROFILE"] == "atlas"
    finally:
        clear_session_vars(tokens)
    assert not any(k.startswith("HERMES_STARTED_BY_") for k in builder({}))


@pytest.mark.parametrize("builder", [hermes_subprocess_env, build_subprocess_env,
                                    lambda: _sanitize_subprocess_env(dict(os.environ))])
def test_nonterminal_builders_never_attribute(monkeypatch, builder):
    monkeypatch.setenv("HERMES_STARTED_BY_SESSION_ID", "stale")
    monkeypatch.setenv("HERMES_STARTED_BY_PROFILE", "stale")
    tokens = set_session_vars(session_id="launcher", profile="default")
    try:
        assert not any(k.startswith("HERMES_STARTED_BY_") for k in builder())
    finally:
        clear_session_vars(tokens)


@pytest.mark.platforms("posix")
def test_background_export_snapshot_excludes_provenance(tmp_path):
    snapshot = tmp_path / "snapshot"
    env = ProcessRegistry._spawn_env({"HERMES_STARTED_BY_SESSION_ID": "old",
                                     "HERMES_LAUNCH_PARENT_ID": "unit-parent"})
    # Explicit command exports must also be excluded, not only inherited values.
    script = ('export HERMES_STARTED_BY_SESSION_ID=parent HERMES_STARTED_BY_PROFILE=default '
              'HERMES_LAUNCH_PARENT_ID=unit HERMES_LAUNCH_TITLE=title; '
              + _export_dump_excluding_session_vars(str(snapshot)))
    subprocess.run(["bash", "-c", script], env=env, check=True)
    saved = snapshot.read_text()
    assert "HERMES_STARTED_BY_" not in saved
    assert "HERMES_LAUNCH_" not in saved
