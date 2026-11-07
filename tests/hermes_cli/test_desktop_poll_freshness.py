"""Desktop polls reuse definitions, not observations of clock or process liveness."""
import json
import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tests.hermes_cli import test_web_server_cron_profiles as profile_fixtures

isolated_profiles = profile_fixtures.isolated_profiles


@pytest.fixture
def activity_store(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli.web_routers import status

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    real_ns = time.time_ns
    monkeypatch.setattr(time, "time_ns", lambda: real_ns() + 10_000_000_000)
    db = SessionDB(db_path=home / "state.db")
    clock = [1794000000.0]
    monkeypatch.setattr(status.time, "time", lambda: clock[0])
    db.create_session("active", source="cli")
    db._write_sql("UPDATE sessions SET started_at=?,last_activity_at=? WHERE id='active'", (clock[0], clock[0]))
    yield db, clock
    db.close()


def test_status_reuses_selection_but_expires_activity_without_writes(activity_store, monkeypatch):
    from hermes_cli.web_routers import status
    from hermes_state import SessionDB

    _db, clock = activity_store
    calls = []
    original = SessionDB.list_sessions_rich

    def counted(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SessionDB, "list_sessions_rich", counted)
    assert status._count_status_active_sessions() == 1
    before = len(calls)
    assert status._count_status_active_sessions() == 1
    assert len(calls) == before, "unchanged status must not rebuild the rich session list"
    clock[0] += 300
    assert status._count_status_active_sessions() == 0
    clock[0] -= 301
    assert status._count_status_active_sessions() == 1


@pytest.mark.parametrize("mutation", ["end", "archive", "hide", "message", "receipt", "replace"])
def test_status_next_call_matches_rich_selection_after_mutation(activity_store, mutation):
    from hermes_cli.web_routers import status
    from hermes_state import SessionDB

    db, clock = activity_store
    assert status._count_status_active_sessions() == 1
    if mutation in {"end", "archive", "hide"}:
        statement = {"end": "ended_at=1", "archive": "archived=1", "hide": "hidden=1"}[mutation]
        db._write_sql(f"UPDATE sessions SET {statement} WHERE id='active'")
    elif mutation == "message":
        clock[0] += 301
        db._write_sql("INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
                      ("active", "user", "fixture", clock[0]))
    elif mutation == "receipt":
        db.create_session("reset", source="cli")
        db._write_sql("UPDATE sessions SET started_at=?,last_activity_at=? WHERE id='reset'", (clock[0], clock[0]))
        db._write_sql("UPDATE sessions SET end_reason='session_reset' WHERE id='active'")
        db._write_sql("INSERT INTO state_meta(key,value) VALUES (?,?)", (
            "gateway.local_policy.v1:active", json.dumps({"session_id": "active", "lineage": ["active", "reset"]})))
    else:
        replacement = db.db_path.with_name("replacement.db")
        other = SessionDB(db_path=replacement)
        other.close()
        db.close()
        os.replace(replacement, db.db_path)
    reader = SessionDB(db_path=db.db_path, read_only=True)
    try:
        rows = reader.list_sessions_rich(limit=50, compact_rows=True)
        expected = sum(row.get("ended_at") is None and clock[0] - row.get("last_active", row.get("started_at", 0)) < 300 for row in rows)
    finally:
        reader.close()
    assert status._count_status_active_sessions() == expected


@pytest.fixture
def cron_reads(isolated_profiles, monkeypatch):
    from hermes_cli.web_routers import cron
    from hermes_cli.web_server_cron import _call_cron_for_profile

    for name, home in isolated_profiles.items():
        path = home / "cron/jobs.json"
        path.write_text(json.dumps({"jobs": [{"id": name, "prompt": "fixture", "enabled": True}]}))
        os.utime(path, (10, 10))
        _call_cron_for_profile(name, "list_jobs", True)
        os.utime(home / "cron/executions.db", (10, 10))
    clock = [1000.0]
    monkeypatch.setattr(cron.time, "time", lambda: clock[0])
    app = FastAPI()
    app.include_router(cron.router)
    with TestClient(app) as client:
        yield client, isolated_profiles, clock


def test_cron_heartbeat_is_current_inside_the_same_revision_window(cron_reads):
    client, homes, clock = cron_reads
    marker = homes["default"] / "cron/ticker_heartbeat"
    marker.write_text("500")
    os.utime(marker, (10, 10))
    first = client.get("/api/cron/jobs?profile=default")
    assert first.json()[0]["scheduler_heartbeat_age_s"] == 500
    clock[0] += 1
    second = client.get("/api/cron/jobs?profile=default", headers={"If-None-Match": first.headers.get("etag", "base")})
    assert second.status_code == 200
    assert second.json()[0]["scheduler_heartbeat_age_s"] == 501


def test_cron_live_owner_death_reveals_older_live_attempt_without_file_write(cron_reads, monkeypatch):
    from cron import executions
    from hermes_cli.web_server_cron import _cron_store_scope

    client, homes, _clock = cron_reads
    with _cron_store_scope(homes["default"]):
        older = executions.create_execution("default", source="fixture")
        newer = executions.create_execution("default", source="fixture")
    live = {older["id"], newer["id"]}
    monkeypatch.setattr(executions, "_row_owner_live", lambda row: row["id"] in live)
    os.utime(homes["default"] / "cron/executions.db", (10, 10))
    first = client.get("/api/cron/jobs?profile=default")
    newest = first.json()[0]["live_execution"]["id"]
    live.remove(newest)
    second = client.get("/api/cron/jobs?profile=default", headers={"If-None-Match": first.headers.get("etag", "base")})
    assert second.status_code == 200
    assert second.json()[0]["live_execution"]["id"] == next(iter(live))
    live.clear()
    assert client.get("/api/cron/jobs?profile=default").json()[0]["live_execution"] is None


def test_cron_definition_reuse_survives_heartbeat_and_ledger_changes(cron_reads, monkeypatch):
    from cron import jobs, executions
    from hermes_cli.web_server_cron import _cron_store_scope

    client, homes, _clock = cron_reads
    assert client.get("/api/cron/jobs?profile=default").status_code == 200
    loads = []
    original = jobs.load_jobs

    def counted():
        loads.append(1)
        return original()

    monkeypatch.setattr(jobs, "load_jobs", counted)
    with _cron_store_scope(homes["default"]):
        execution = executions.create_execution("default", source="fixture", job=None)
    (homes["default"] / "cron/ticker_heartbeat").write_text("900")
    response = client.get("/api/cron/jobs?profile=default")
    assert response.json()[0]["latest_execution"]["id"] == execution["id"]
    assert response.json()[0]["scheduler_heartbeat_age_s"] == 100
    assert loads == [], "ledger and heartbeat observations must not reload unchanged job definitions"


@pytest.mark.parametrize("mutation", ["edit", "pause", "add", "delete", "replace", "same_mtime"])
def test_cron_definition_mutation_is_fresh_and_wire_identical(cron_reads, mutation):
    from hermes_cli.web_routers import cron

    client, homes, _clock = cron_reads
    assert client.get("/api/cron/jobs?profile=default").status_code == 200
    path = homes["default"] / "cron/jobs.json"
    before = path.stat()
    payload = json.loads(path.read_text())
    if mutation == "pause":
        payload["jobs"][0]["enabled"] = False
    elif mutation == "add":
        payload["jobs"].append({"id": "second", "prompt": "new"})
    elif mutation == "delete":
        payload["jobs"] = []
    else:
        payload["jobs"][0]["prompt"] = "changed"
    target = path.with_name("new.json") if mutation == "replace" else path
    target.write_text(json.dumps(payload))
    if mutation == "replace":
        target.replace(path)
    if mutation == "same_mtime":
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    response = client.get("/api/cron/jobs?profile=default")
    expected = cron._list_cron_jobs_sync("default")
    assert response.content == JSONResponse(content=jsonable_encoder(expected)).body
    assert len(expected) == len(payload["jobs"])
    if expected:
        assert expected[0]["prompt"] == payload["jobs"][0]["prompt"]
        assert expected[0]["enabled"] == payload["jobs"][0]["enabled"]


def test_cron_inprocess_result_cannot_mutate_the_cache(cron_reads):
    from hermes_cli.web_server_cron import _call_cron_for_profile

    _client, _homes, _clock = cron_reads
    first = _call_cron_for_profile("default", "list_jobs", True)
    first[0]["prompt"] = "not persisted"
    assert _call_cron_for_profile("default", "list_jobs", True)[0]["prompt"] == "fixture"


def test_cron_wire_hit_preserves_nested_keys_unicode_and_field_order(cron_reads):
    from cron import jobs
    from hermes_cli.web_server_cron import _annotate_cron_job, _cron_store_scope

    client, homes, _clock = cron_reads
    payload = {"jobs": [{"id": "default", "prompt": 'quote " and \\n snowman ☃',
                         "extra": {"live_execution": {"arbitrary": True}, "scheduler_heartbeat_age_s": [1, 2]},
                         "live_execution": "legacy", "scheduler_heartbeat_age_s": 999}]}
    (homes["default"] / "cron/jobs.json").write_text(json.dumps(payload))
    client.get("/api/cron/jobs?profile=default")
    response = client.get("/api/cron/jobs?profile=default")
    with _cron_store_scope(homes["default"]):
        reference = [_annotate_cron_job(job, "default", homes["default"], jobs.get_ticker_heartbeat_age())
                     for job in jobs.list_jobs(True)]
    assert response.content == JSONResponse(content=jsonable_encoder(reference)).body


def test_status_does_not_publish_a_snapshot_built_across_a_write(activity_store, monkeypatch):
    from hermes_cli.web_routers import status
    from hermes_state import SessionDB

    db, _clock = activity_store
    original = SessionDB.list_sessions_rich
    writes = []

    def raced(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not writes:
            db._write_sql("UPDATE sessions SET ended_at=1 WHERE id='active'")
            writes.append(1)
        return result

    monkeypatch.setattr(SessionDB, "list_sessions_rich", raced)
    assert status._count_status_active_sessions() == 1
    assert status._count_status_active_sessions() == 0


def test_cron_partial_read_failure_is_retried_without_file_changes(cron_reads, monkeypatch):
    from cron import jobs
    from hermes_constants import get_hermes_home

    client, homes, _clock = cron_reads
    path = homes["default"] / "cron/jobs.json"
    path.write_text(json.dumps({"jobs": [{"id": "default", "prompt": "changed"}]}))
    original = jobs.load_jobs
    failed = []

    def flaky():
        if get_hermes_home() == homes["default"] and not failed:
            failed.append(1)
            raise OSError("fixture transient read failure")
        return original()

    monkeypatch.setattr(jobs, "load_jobs", flaky)
    first = client.get("/api/cron/jobs")
    assert {job["id"] for job in first.json()} == {"worker_alpha"}
    second = client.get("/api/cron/jobs")
    assert {job["id"] for job in second.json()} == {"default", "worker_alpha"}


def _freeze_same_tick_metadata(monkeypatch, paths):
    original = Path.stat
    stamps = {}
    for path in paths:
        try:
            stamps[str(path)] = original(path)
        except FileNotFoundError:
            stamps[str(path)] = None
    latest = max(stamp.st_mtime_ns for stamp in stamps.values() if stamp is not None)
    monkeypatch.setattr(time, "time_ns", lambda: latest + 1)

    def stat(path, *args, **kwargs):
        if str(path) in stamps:
            saved = stamps[str(path)]
            if saved is None:
                raise FileNotFoundError(path)
            return saved
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)


def test_status_racy_metadata_does_not_hide_a_same_tick_write(activity_store, monkeypatch):
    from hermes_cli.web_routers import status

    db, _clock = activity_store
    _freeze_same_tick_metadata(monkeypatch, [db.db_path, Path(str(db.db_path) + "-wal")])
    assert status._count_status_active_sessions() == 1
    db._write_sql("UPDATE sessions SET ended_at=1 WHERE id='active'")
    assert status._count_status_active_sessions() == 0


def test_cron_racy_metadata_does_not_hide_a_same_tick_edit(cron_reads, monkeypatch):
    client, homes, _clock = cron_reads
    path = homes["default"] / "cron/jobs.json"
    _freeze_same_tick_metadata(monkeypatch, [path])
    assert client.get("/api/cron/jobs?profile=default").json()[0]["prompt"] == "fixture"
    path.write_text(json.dumps({"jobs": [{"id": "default", "prompt": "changed", "enabled": True}]}))
    assert client.get("/api/cron/jobs?profile=default").json()[0]["prompt"] == "changed"
