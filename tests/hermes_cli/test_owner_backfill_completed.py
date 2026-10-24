"""A completed migration is a read, even when another process owns writes."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hermes_state import SessionDB
from hermes_cli.web_routers import sessions


def test_completed_owner_backfill_does_not_attempt_refused_maintenance(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("owned", "desktop", profile_name="default")
    monkeypatch.setattr(sessions, "_serving_profile", lambda profile: "default")
    monkeypatch.setattr(sessions, "_open_session_db_for_profile",
                        lambda profile, *, read_only: SessionDB(db_path=path, read_only=read_only))
    attempts = []

    def refused(profile, run):
        attempts.append(profile)
        raise HTTPException(409, "Exclusive maintenance refused")

    monkeypatch.setattr("hermes_cli.web_server_sessions._with_session_maintenance", refused)
    app = FastAPI()
    app.include_router(sessions.manage_router)
    with TestClient(app) as client:
        for _ in range(3):
            response = client.post("/api/sessions/owner-backfill", json={})
            assert response.status_code == 200, response.text
            assert response.json() == {"ok": True, "stamped": 0, "profile": "default"}
    assert attempts == []


def test_unowned_rows_still_require_owner_authority(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("legacy", "desktop")
        db._conn.execute("UPDATE sessions SET profile_name=NULL")
        db._conn.commit()
    monkeypatch.setattr(sessions, "_serving_profile", lambda profile: "default")
    monkeypatch.setattr(sessions, "_open_session_db_for_profile",
                        lambda profile, *, read_only: SessionDB(db_path=path, read_only=read_only))

    def refused(profile, run):
        raise HTTPException(409, "Exclusive maintenance refused")

    monkeypatch.setattr("hermes_cli.web_server_sessions._with_session_maintenance", refused)
    app = FastAPI()
    app.include_router(sessions.manage_router)
    with TestClient(app) as client:
        assert client.post("/api/sessions/owner-backfill", json={}).status_code == 409
    with SessionDB(db_path=path, read_only=True) as db:
        assert db.get_session("legacy")["profile_name"] is None
