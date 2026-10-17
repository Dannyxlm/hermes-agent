"""Creation pins and shared-board isolation, exercised through public surfaces."""
import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "board"))
    home.joinpath("config.yaml").write_text("kanban:\n  require_model: true\n")
    kb.init_db()
    return home


def create(surface, **fields):
    if surface == "cli":
        flags = " ".join(f'--{key} {value}' for key, value in fields.items())
        return cli.run_slash(f'create "build" --assignee default --json {flags}')
    if surface == "tool":
        return kanban_tools._handle_create({"title": "build", "assignee": "default", **fields})
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard import plugin_api

    app = FastAPI()
    app.include_router(plugin_api.router)
    payload = {f"{key}_override": value for key, value in fields.items()}
    response = TestClient(app).post("/tasks", json={"title": "build", **payload})
    return response.text


@pytest.mark.parametrize("surface", ["cli", "tool", "api"])
def test_creation_requires_model_and_snapshots_board_default(board, surface):
    error = create(surface)
    assert "no model for this card" in error
    assert "--model" in error
    assert "kanban.board_models" in error
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []
    board.joinpath("config.yaml").write_text(
        "kanban:\n  require_model: true\n  board_models:\n"
        "    default:\n      model: budget-model\n      provider: test-provider\n")
    create(surface)
    board.joinpath("config.yaml").write_text("kanban:\n  require_model: true\n")
    with kbc.connect_closing() as conn:
        task = kb.list_tasks(conn)[0]
        assert (task.model_override, task.provider_override) == ("budget-model", "test-provider")
        created = next(event for event in kb.list_events(conn, task.id) if event.kind == "created")
        assert created.payload["model_override"] == "budget-model"
    create(surface, model="explicit-model", provider="explicit-provider")
    with kbc.connect_closing() as conn:
        assert sorted((task.model_override, task.provider_override) for task in kb.list_tasks(conn)) == [
            ("budget-model", "test-provider"), ("explicit-model", "explicit-provider")]


def test_new_board_is_strict_and_running_pin_is_refused(board):
    board.joinpath("config.yaml").write_text("{}\n")
    kb.create_board("new-board")
    with kbc.connect_closing(board="new-board") as conn:
        with pytest.raises(ValueError, match="no model for this card"):
            kb.create_task(conn, title="missing pin", board="new-board")
        tid = kb.create_task(conn, title="pinned", model_override="budget-model", assignee="default", board="new-board")
        kb.claim_task(conn, tid)
        with pytest.raises(RuntimeError, match="running worker keeps its model"):
            kb.set_model_override(conn, tid, "late-model")
        assert kb.get_task(conn, tid).model_override == "budget-model"
    board.joinpath("config.yaml").write_text("kanban:\n  require_model: false\n")
    with kbc.connect_closing(board="new-board") as conn:
        assert kb.create_task(conn, title="explicit compatibility mode", board="new-board")


@pytest.mark.parametrize("surface", ["cli", "tool", "api", "db"])
def test_isolated_home_cannot_write_shared_board(tmp_path, monkeypatch, surface):
    import hermes_constants

    shared = tmp_path / "shared"
    shared.mkdir()
    sandbox = shared / "smoke-test"
    sandbox.mkdir()
    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: shared)
    monkeypatch.setenv("HERMES_HOME", str(sandbox))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    if surface == "db":
        with pytest.raises(PermissionError, match="set HERMES_KANBAN_HOME to isolate Kanban"):
            kbc.connect()
    else:
        error = create(surface, model="budget-model")
        assert "set HERMES_KANBAN_HOME to isolate Kanban" in error
    assert not (shared / "kanban.db").exists()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "isolated-board"))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="safe", model_override="budget-model")
        assert kb.get_task(conn, tid).title == "safe"
    monkeypatch.delenv("HERMES_KANBAN_HOME")
    monkeypatch.setenv("HERMES_HOME", str(shared / "profiles" / "builder"))
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="shared on purpose", model_override="budget-model")
        assert kb.get_task(conn, tid).title == "shared on purpose"


@pytest.mark.parametrize("surface", ["cli", "tool", "api"])
def test_fresh_default_board_requires_pin_without_configuration(tmp_path, monkeypatch, surface):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "new-board"))
    assert "no model for this card" in create(surface)
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_board_defaults_follow_connection_not_current_board(board):
    board.joinpath("config.yaml").write_text(
        "kanban:\n  board_models:\n    other:\n      model: other-model\n      provider: other-provider\n")
    kb.create_board("other")
    with kbc.connect_closing(board="other") as conn:
        tid = kb.create_task(conn, title="connection scoped")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert (task.model_override, task.provider_override) == ("other-model", "other-provider")


def test_late_pin_error_reaches_cli_and_dashboard(board):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard import plugin_api

    app = FastAPI()
    app.include_router(plugin_api.router)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="running", model_override="budget-model", assignee="default")
        kb.claim_task(conn, tid)
    assert "running worker keeps its model" in cli.run_slash(f"set-model {tid} late-model")
    response = TestClient(app).patch(f"/tasks/{tid}", json={"model_override": "late-model"})
    assert response.status_code == 400
    assert "running worker keeps its model" in response.json()["detail"]
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.model_override == "budget-model"
