"""/api/analytics/usage serves tool/skill tallies from the per-state.db cache (R8-06b)."""
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.web_routers import analytics
from hermes_state import SessionDB


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_state
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(hermes_state, "_default_db_path", lambda: db_path)
    monkeypatch.setattr(analytics, "_usage_caches", {})
    db = SessionDB(db_path=db_path)
    db.create_session("s1", source="cli", model="m")
    db.append_message("s1", role="assistant", content="x", tool_calls=[
        {"function": {"name": "skill_view", "arguments": json.dumps({"name": "github-pr-workflow"})}},
        {"function": {"name": "terminal", "arguments": "{}"}},
    ])
    db.append_message("s1", role="tool", content="r", tool_name="terminal")
    db.close()
    app = FastAPI()
    app.include_router(analytics.router)
    return TestClient(app), db_path


def test_usage_route_reuses_cache_and_picks_up_new_rows(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    first = client.get("/api/analytics/usage?days=7").json()
    assert {t["tool"]: t["count"] for t in first["tools"]} == {"skill_view": 1, "terminal": 1}
    assert first["skills"]["summary"]["total_skill_loads"] == 1
    assert len(analytics._usage_caches) == 1
    cache = next(iter(analytics._usage_caches.values()))
    assert cache.last_refreshed == 1
    assert (tmp_path / "cache" / "usage-tallies.json").exists()

    assert client.get("/api/analytics/usage?days=30").json()["tools"] == first["tools"]
    assert cache.last_refreshed == 0
    assert len(analytics._usage_caches) == 1, "one cache per state.db, shared by every window"

    db = SessionDB(db_path=db_path)
    db.append_message("s1", role="assistant", content="y", tool_calls=[{"function": {"name": "terminal", "arguments": "{}"}}])
    db.close()
    third = client.get("/api/analytics/usage?days=7").json()
    assert cache.last_refreshed == 1
    assert {t["tool"]: t["count"] for t in third["tools"]}["terminal"] == 2


def test_usage_payload_shape_unchanged(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    data = client.get("/api/analytics/usage?days=7").json()
    assert set(data) == {"daily", "by_model", "by_task", "totals", "period_days", "skills", "tools"}
    assert set(data["tools"][0]) == {"tool", "count", "percentage"}
    assert set(data["skills"]) == {"summary", "top_skills"}
    assert set(data["skills"]["top_skills"][0]) == {
        "skill", "view_count", "manage_count", "total_count", "percentage", "last_used_at"}
