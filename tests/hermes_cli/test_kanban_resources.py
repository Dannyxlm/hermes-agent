"""User-facing Kanban resource policy and OOM recovery contracts."""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_resources as policy


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(policy, "_capacity_bytes", lambda: 32 * 1024**3)
    kb.init_db()
    return home


def create_card(options=""):
    return json.loads(cli.run_slash(f'create "build suite" --assignee default --json {options}'))["id"]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("options,expected", [("", "MemoryMax=4294967296"),
                                               ("--resource-class heavy", "MemoryMax=25769803776")])
def test_cli_card_dispatch_uses_named_memory_budget(board, monkeypatch, options, expected):
    captured = []
    monkeypatch.setenv("INVOCATION_ID", "gateway")
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv", lambda: ["hermes"])

    def popen(argv, **kwargs):
        captured.append(argv)
        return Mock(pid=4321)

    monkeypatch.setattr(subprocess, "Popen", popen)
    tid = create_card(options)
    with kbc.connect() as conn:
        result = dispatch.dispatch_once(conn)
        assert [item[0] for item in result.spawned] == [tid]
        task = kb.get_task(conn, tid)
        run = kb.list_runs(conn, tid)[0]
    assert expected in captured[0]
    assert "--collect" not in captured[0]  # failed scopes retain Result until persisted
    assert run.metadata["worker_resources"]["resource_class"] == (task.resource_class or "standard")


def test_board_default_edit_override_and_clear(board):
    board.joinpath("config.yaml").write_text("kanban:\n  board_resource_classes:\n    default: heavy\n")
    tid = create_card()
    with kbc.connect() as conn:
        assert policy.resolve_worker_resources(kb.get_task(conn, tid)).memory_max_bytes == 25769803776
    assert "Edited" in cli.run_slash(f"edit {tid} --resource-class standard")
    with kbc.connect() as conn:
        assert policy.resolve_worker_resources(kb.get_task(conn, tid)).memory_max_bytes == 4294967296
    assert "Edited" in cli.run_slash(f'edit {tid} --resource-class ""')
    shown = json.loads(cli.run_slash(f"show {tid} --json"))
    assert shown["task"]["resource_class"] is None
    with kbc.connect() as conn:
        assert policy.resolve_worker_resources(kb.get_task(conn, tid)).memory_max_bytes == 25769803776


def test_custom_class_config_is_read_by_dispatcher(board):
    board.joinpath("config.yaml").write_text(
        "kanban:\n  default_resource_class: test\n  resource_classes:\n"
        "    test:\n      memory_max_mb: 8192\n      ram_fraction: 0.75\n")
    tid = create_card()
    with kbc.connect() as conn:
        assert policy.resolve_worker_resources(kb.get_task(conn, tid)).memory_max_bytes == 8589934592


@pytest.mark.parametrize("spec", ["ram_fraction: 1", "ram_fraction: .nan", "memory_max_mb: 2"])
def test_unsafe_class_is_rejected_before_spawn(board, spec):
    board.joinpath("config.yaml").write_text(f"kanban:\n  resource_classes:\n    heavy:\n      {spec}\n")
    tid = create_card("--resource-class heavy")
    with kbc.connect() as conn:
        with pytest.raises(ValueError, match="resource class heavy"):
            policy.resolve_worker_resources(kb.get_task(conn, tid))


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("scope_result,outcome", [("oom-kill", "oom_killed"), ("success", "crashed")])
def test_dead_worker_records_cap_and_remedy_not_just_sigkill(board, monkeypatch, scope_result, outcome):
    tid = create_card()
    with kbc.connect() as conn:
        task = kb.claim_task(conn, tid)
        policy.prepare_worker_resources(conn, task, "default")
        dispatch._set_worker_pid(conn, tid, 4321)
        # A subsequent config/card edit must not rewrite the explanation of this run.
        kb.edit_task(conn, tid, resource_class="heavy")
        board.joinpath("config.yaml").write_text("kanban:\n  resource_classes:\n    standard:\n      memory_max_mb: 2048\n")
        monkeypatch.setattr(dispatch, "_worker_alive", lambda *args: False)
        monkeypatch.setattr(dispatch, "_classify_worker_exit", lambda pid: ("signaled", 9))
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
        commands = []

        def run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=scope_result, stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        assert dispatch.detect_crashed_workers(conn) == [tid]
        run_record = kb.list_runs(conn, tid)[0]
        task = kb.get_task(conn, tid)
        assert run_record.outcome == outcome
        if scope_result == "oom-kill":
            reason = "worker OOM-killed with the 4 GiB memory cap (resource class standard); rerun with resource class heavy"
            assert run_record.error == reason
            assert task.last_failure_error == reason
            assert task.status == "blocked"
            assert run_record.metadata["worker_resources"]["memory_max_bytes"] == 4294967296
            assert commands[-1][:3] == ["systemctl", "--user", "reset-failed"]
        else:
            assert task.last_failure_error == "pid 4321 killed by signal 9"


def test_completion_preserves_dispatch_budget_evidence(board):
    tid = create_card("--resource-class heavy")
    with kbc.connect() as conn:
        task = kb.claim_task(conn, tid)
        policy.prepare_worker_resources(conn, task, "default")
        assert kb.complete_task(conn, tid, result="done", metadata={
            "worker_resources": {"memory_max_bytes": 1}, "verified": True,
        })
        assert kb.edit_task(conn, tid, result="backfilled", metadata={"verified": True})
        run = kb.list_runs(conn, tid)[0]
    assert run.metadata["worker_resources"]["memory_max_bytes"] == 25769803776
    assert run.metadata["verified"] is True


def test_dashboard_api_create_and_edit_resource_class(board):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard import plugin_api

    app = FastAPI()
    app.include_router(plugin_api.router)
    client = TestClient(app)
    response = client.post("/tasks", json={"title": "api build", "resource_class": "heavy"})
    assert response.status_code == 200
    tid = response.json()["task"]["id"]
    assert response.json()["task"]["resource_class"] == "heavy"
    response = client.patch(f"/tasks/{tid}", json={"resource_class": "standard"})
    assert response.status_code == 200
    assert response.json()["task"]["resource_class"] == "standard"
    response = client.patch(f"/tasks/{tid}", json={"resource_class": ""})
    assert response.status_code == 200
    assert response.json()["task"]["resource_class"] is None
    assert client.patch(f"/tasks/{tid}", json={"resource_class": "typo"}).status_code == 400
