"""Conditional cron lists avoid re-reading unchanged large job records."""
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cron import jobs
from hermes_cli.web_routers import cron
from tests.hermes_cli import test_web_server_cron_profiles as profile_fixtures

isolated_profiles = profile_fixtures.isolated_profiles


@pytest.fixture
def cron_client(isolated_profiles, monkeypatch):
    paths = {}
    for profile, home in isolated_profiles.items():
        path = home / "cron" / "jobs.json"
        path.write_text(json.dumps({"jobs": [{"id": profile, "prompt": "a large job " * 30000}]}))
        os.utime(path, (10, 10))
        paths[profile] = path
        # Initialize the independent execution ledger before taking a settled validator.
        from hermes_cli.web_server_cron import _call_cron_for_profile
        _call_cron_for_profile(profile, "list_jobs", True)
        os.utime(home / "cron" / "executions.db", (10, 10))
    app = FastAPI()
    app.include_router(cron.router)
    loads = []
    load = jobs.load_jobs

    def counted(*args, **kwargs):
        loads.append(1)
        return load(*args, **kwargs)

    monkeypatch.setattr(jobs, "load_jobs", counted)
    # Freeze the observation interval away from a boundary; production heartbeats still
    # expire the weak validator on the next 30-second observation window.
    monkeypatch.setattr(cron.time, "time", lambda: 1000.0)
    with TestClient(app) as client:
        yield client, paths, loads


def test_unchanged_cron_list_is_bodyless_without_job_reads(cron_client):
    client, _paths, loads = cron_client
    first = client.get("/api/cron/jobs")
    assert first.status_code == 200
    assert {row["id"] for row in first.json()} == {"default", "worker_alpha"}
    before = len(loads)
    # Base lacks ETag: use a harmless sentinel so RED reaches the existing route.
    tag = first.headers.get("etag", 'W/"base-has-no-validator"')
    second = client.get("/api/cron/jobs", headers={"If-None-Match": tag})
    assert second.status_code == 304
    assert second.content == b""
    assert len(loads) == before
    assert second.headers["etag"] == tag
    assert "private" in second.headers["cache-control"]


def test_cron_validator_cannot_hide_file_or_profile_changes(cron_client):
    client, paths, _loads = cron_client
    first = client.get("/api/cron/jobs?profile=default")
    tag = first.headers.get("etag", 'W/"base-has-no-validator"')
    other = client.get("/api/cron/jobs?profile=worker_alpha", headers={"If-None-Match": tag})
    assert other.status_code == 200
    assert [row["id"] for row in other.json()] == ["worker_alpha"]
    paths["default"].write_text('{"jobs": []}')
    updated = client.get("/api/cron/jobs?profile=default", headers={"If-None-Match": tag})
    assert updated.status_code == 200 and updated.json() == []
    assert client.get("/api/cron/jobs?profile=missing", headers={"If-None-Match": tag}).status_code == 404


def test_cron_heartbeat_age_does_not_freeze_behind_validator(cron_client, monkeypatch):
    client, paths, _loads = cron_client
    marker = paths["default"].parent / "ticker_heartbeat"
    marker.write_text("500")
    os.utime(marker, (10, 10))
    first = client.get("/api/cron/jobs?profile=default")
    assert first.status_code == 200
    assert first.json()[0]["scheduler_heartbeat_age_s"] == 500
    tag = first.headers.get("etag", 'W/"base-has-no-validator"')
    monkeypatch.setattr(cron.time, "time", lambda: 1031.0)
    later = client.get("/api/cron/jobs?profile=default", headers={"If-None-Match": tag})
    assert later.status_code == 200
    assert later.json()[0]["scheduler_heartbeat_age_s"] == 531


def test_execution_only_transition_invalidates_cron_list(cron_client):
    from cron import executions
    from hermes_cli.web_server_cron import _cron_store_scope

    client, paths, _loads = cron_client
    home = paths["default"].parent.parent
    with _cron_store_scope(home):
        execution = executions.create_execution("default", source="fixture")
    ledger = home / "cron" / "executions.db"
    os.utime(ledger, (10, 10))
    first = client.get("/api/cron/jobs?profile=default")
    assert first.status_code == 200
    assert first.json()[0]["latest_execution"]["status"] == "claimed"
    tag = first.headers.get("etag", 'W/"base-has-no-validator"')
    with _cron_store_scope(home):
        executions.mark_execution_running(execution["id"])
    changed = client.get("/api/cron/jobs?profile=default", headers={"If-None-Match": tag})
    assert changed.status_code == 200
    assert changed.json()[0]["latest_execution"]["status"] == "running"
