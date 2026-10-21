"""Versioned board attention never mistakes agent review for an owner request."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from contextlib import closing
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board_client(tmp_path, monkeypatch):
    from hermes_cli import profiles
    root = tmp_path / "installation"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)
    (root / "config.yaml").write_text(json.dumps({"cloudseed_mobile": {
        "owner_user_id": "owner", "owner_provider": "basic"}}))
    kb.init_db()
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_attention_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    app = FastAPI()

    @app.middleware("http")
    async def verified_identity(request, call_next):
        user = request.headers.get("x-test-user", "owner")
        if user != "missing":
            request.state.session = SimpleNamespace(user_id=user, provider="basic")
        return await call_next(request)

    app.include_router(module.router, prefix="/api/plugins/kanban")
    with TestClient(app) as client:
        yield client


def tasks(response):
    return {t["id"]: t for column in response["columns"] for t in column["tasks"]}


def test_versioned_board_keeps_unknown_asks_unknown_and_known_waits_false(board_client):
    with closing(kbc.connect()) as conn:
        review = kb.create_task(conn, title="Agent review", assignee="iris")
        unbound = kb.create_task(conn, title="Owner input without principal", assignee="default")
        dependency = kb.create_task(conn, title="Dependency")
        ordinary = kb.create_task(conn, title="Ordinary")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
        conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (unbound,))
        conn.execute("UPDATE tasks SET status='todo',block_kind='dependency' WHERE id=?", (dependency,))
        conn.commit()
    response = board_client.get("/api/plugins/kanban/board?board=default&presentation_version=1")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result.get("presentation_version") == 1, result
    assert result["attention_complete"] is False
    cards = tasks(result)
    assert cards[review]["owner_attention"] is None
    assert cards[unbound]["owner_attention"] is None
    assert cards[dependency]["owner_attention"] is False
    assert cards[ordinary]["owner_attention"] is False
    assert all(c["attention_reason"] is None and c["attention_chat"] is None for c in cards.values())
    legacy = board_client.get("/api/plugins/kanban/board?board=default").json()
    assert "presentation_version" not in legacy
    assert "owner_attention" not in tasks(legacy)[ordinary]


@pytest.mark.parametrize("user,status", [("foreign", 403), ("missing", 401)])
def test_versioned_projection_requires_configured_owner(board_client, user, status):
    url = "/api/plugins/kanban/board?board=default&presentation_version=1"
    response = board_client.get(url, headers={"x-test-user": user})
    assert response.status_code == status, response.text
    assert board_client.get("/api/plugins/kanban/board?board=default", headers={"x-test-user": user}).status_code == 200


def test_activity_is_persisted_card_activity_not_worker_heartbeat(board_client):
    with closing(kbc.connect()) as conn:
        card = kb.create_task(conn, title="Card")
        conn.execute("UPDATE tasks SET status='running',created_at=10,started_at=20 WHERE id=?", (card,))
        conn.execute("UPDATE task_events SET created_at=30 WHERE task_id=?", (card,))
        conn.execute("INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?, 'owner','private',40)", (card,))
        conn.commit()
        assert kbd.heartbeat_worker(conn, card)
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='heartbeat'",
                            (card,)).fetchone()[0] == 1
    response = board_client.get("/api/plugins/kanban/board?presentation_version=1")
    assert response.status_code == 200, response.text
    assert tasks(response.json())[card].get("activity_at") == 40


def test_breaker_block_with_stale_dependency_kind_keeps_attention_unknown(board_client):
    with closing(kbc.connect()) as conn:
        parent = kb.create_task(conn, title="Parent")
        child = kb.create_task(conn, title="Child")
        kb.link_tasks(conn, parent, child)
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()
        assert kb.block_task(conn, child, kind="dependency")
        waiting = kb.get_task(conn, child)
        assert waiting is not None and waiting.status == "todo"
        assert kb.complete_task(conn, parent, result="Parent prerequisite finished and verified.")
        kb.recompute_ready(conn)
        ready = kb.get_task(conn, child)
        assert ready is not None and ready.status == "ready"
        assert kbd._record_task_failure(conn, child, "worker crashed", outcome="crashed", force_trip=True)
        stored = kb.get_task(conn, child)
        assert stored is not None and stored.status == "blocked" and stored.block_kind == "dependency"
    response = board_client.get("/api/plugins/kanban/board?presentation_version=1")
    assert response.status_code == 200, response.text
    result = response.json()
    assert tasks(result)[child]["owner_attention"] is None
    assert result["attention_complete"] is False


def test_running_card_cannot_claim_no_owner_ask_without_coverage(board_client):
    with closing(kbc.connect()) as conn:
        card = kb.create_task(conn, title="In progress")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (card,))
        conn.commit()
    response = board_client.get("/api/plugins/kanban/board?presentation_version=1")
    assert response.status_code == 200, response.text
    assert tasks(response.json())[card]["status"] == "running"
    assert tasks(response.json())[card]["owner_attention"] is None
    assert response.json()["attention_complete"] is False


def test_unknown_status_stays_visible_under_its_own_column(board_client):
    with closing(kbc.connect()) as conn:
        card = kb.create_task(conn, title="Future card")
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE tasks SET status='future-state' WHERE id=?", (card,))
        conn.commit()
    response = board_client.get("/api/plugins/kanban/board?presentation_version=1")
    assert response.status_code == 200, response.text
    columns = {c["name"]: c["tasks"] for c in response.json()["columns"]}
    assert card in [t["id"] for t in columns.get("future-state", [])]
    assert tasks(response.json())[card]["owner_attention"] is None


def test_projection_does_not_change_other_board_or_tenant_counts(board_client):
    with closing(kbc.connect()) as conn:
        a = kb.create_task(conn, title="First", tenant="one")
        b = kb.create_task(conn, title="Second", tenant="two")
    response = board_client.get("/api/plugins/kanban/board?board=default&tenant=one&presentation_version=1")
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(tasks(result)) == {a}
    assert result.get("attention_complete") is True
    assert result["tenants"] == ["one", "two"]
    assert b not in tasks(result)
