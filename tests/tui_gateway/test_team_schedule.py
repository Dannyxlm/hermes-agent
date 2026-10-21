"""Owner-scoped Team and overnight receipts through the public RPC dispatcher."""
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


class OwnerPeer:
    def __init__(self, identity=None):
        self.auth_identity = identity or {"provider": "basic", "user_id": "owner"}
        self.frames = []
        self.ready = threading.Event()

    def write(self, frame):
        self.frames.append(frame)
        self.ready.set()
        return True


@pytest.fixture
def homes(tmp_path, monkeypatch):
    from hermes_cli import config, config_backups, profiles
    monkeypatch.setattr(config, "ensure_hermes_home", lambda: None)
    monkeypatch.setattr(config_backups, "backup_config", lambda *args, **kwargs: None)
    root = tmp_path / "installation"
    named = root / "profiles" / "ops"
    named.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)
    monkeypatch.setattr(server, "_hermes_home", root)
    monkeypatch.setattr(server, "_sessions", {})
    (root / "config.yaml").write_text(json.dumps({"cloudseed_mobile": {
        "owner_provider": "basic", "owner_user_id": "owner"}}))
    (named / "config.yaml").write_text(json.dumps({"cloudseed_mobile": {
        "owner_provider": "basic", "owner_user_id": "foreign"}}))
    for home in (root, named):
        with SessionDB(db_path=home / "state.db") as db:
            db.create_session("same-id", "desktop")
            db._conn.execute("UPDATE sessions SET title=?, last_activity_at=100", (home.name,))
            db.create_session("worker", "kanban")
            db._conn.execute("UPDATE sessions SET last_activity_at=9999999999 WHERE id='worker'")
    peer = OwnerPeer()
    token = bind_transport(peer)
    yield root, named, peer
    reset_transport(token)


def rpc(method, **params):
    return server.handle_request({"id": "team-test", "method": method, "params": params})


def summary(**extra):
    return rpc("cron.summary", **{"profiles": ["default", "ops"], "timezone": "UTC",
                                 "start": 0, "end": 1000, "limit": 1, **extra})


def ledger(home):
    from cron.executions import _initialize_schema
    path = home / "cron" / "executions.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    _initialize_schema(conn)
    conn.commit()
    return conn


def attempt(conn, ident, *, status="completed", scheduled="1970-01-01T00:01:40+00:00",
            claimed="1970-01-01T00:01:41+00:00", delivery=None):
    conn.execute("""INSERT INTO executions
        (id,job_id,source,process_id,pid,process_started_at,status,claimed_at,started_at,
         finished_at,scheduled_instant,delivery_outcome,error)
        VALUES (?, 'daily', 'scheduled', 'fixture', ?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
        (ident, os.getpid(), status, claimed, claimed,
         "1970-01-01T00:01:50+00:00" if status == "completed" else None,
         scheduled, delivery, "private error body"))
    conn.commit()


def test_team_positive_runtime_is_exact_profile_and_idle_is_unknown(homes):
    root, named, _ = homes
    server._sessions["runtime"] = {"profile_home": str(named), "session_key": "same-id", "running": True}
    result = rpc("team.activity", profiles=["default", "ops"])
    assert "result" in result, result
    result = result["result"]
    assert result["version"] == 1 and result["complete"] is False
    assert result["working_count"] is None
    first, second = result["agents"]
    assert first == {"profile": "default", "working": None, "last_human_chat_at": 100, "current": None}
    assert second["working"] is True
    assert second["current"]["title"] == "ops"
    assert second["current"]["started_at"] is None
    assert second["current"]["open_chat"] == {"profile": "ops", "stored_session_id": "same-id"}
    assert "private" not in str(result) and str(root) not in str(result)


@pytest.mark.parametrize("source,title,archived", [("kanban", "Worker", 0),
                                                   ("cron", "Job", 0),
                                                   ("desktop", "Bot Chat", 0),
                                                   ("desktop", "Old", 1),
                                                   ("subagent", "Child", 0),
                                                   ("telegram", "Messaging", 0)])
def test_team_never_offers_worker_canonical_or_archived_chat(homes, source, title, archived):
    _, named, _ = homes
    with SessionDB(db_path=named / "state.db") as db:
        db._conn.execute("UPDATE sessions SET source=?,title=?,archived=? WHERE id='same-id'",
                         (source, title, archived))
    server._sessions["runtime"] = {"profile_home": str(named), "session_key": "same-id", "running": True}
    response = rpc("team.activity", profiles=["ops"])
    assert "result" in response, response
    assert response["result"]["agents"][0]["current"]["open_chat"] is None


@pytest.mark.parametrize("method", ["team.activity", "cron.summary"])
@pytest.mark.parametrize("identity", [None, {"provider": "basic", "user_id": "foreign"},
                                      {"provider": "foreign", "user_id": "owner"}])
def test_owner_admission_uses_root_not_profile_config(homes, method, identity):
    homes[2].auth_identity = identity
    response = rpc(method, **({"profiles": ["ops"]} if method == "team.activity" else {
        "profiles": ["ops"], "timezone": "UTC", "start": 0, "end": 1000}))
    assert response.get("error", {}).get("code") == 4403, response


@pytest.mark.parametrize("profiles", [[], ["ops", "ops"], ["../ops"], ["OPS"], [""], ["missing"], ["default"] * 101])
def test_profile_set_is_validated_before_any_reads(homes, profiles):
    response = rpc("team.activity", profiles=profiles)
    assert response.get("error", {}).get("code") in (4002, 4064), response
    assert not (homes[0] / "profiles" / "missing").exists()


def test_missing_ledger_is_partial_not_zero_success(homes):
    response = summary()
    assert "result" in response, response
    result = response["result"]
    assert result["complete"] is False and result["runs"] == []
    assert result["counts"] == {"expected": None, "succeeded": None, "retried": None, "needs_attention": None}
    assert not (homes[0] / "cron").exists()
    assert not (homes[1] / "cron").exists()


def test_cron_pages_durable_attempts_without_inventing_retry_or_delivery(homes):
    with ledger(homes[0]) as conn:
        attempt(conn, "a", delivery=None)
        attempt(conn, "b", status="failed", claimed="1970-01-01T00:02:00+00:00")
    with ledger(homes[1]) as conn:
        attempt(conn, "a", scheduled="1970-01-01T00:03:00+00:00")
    first = summary()
    assert "result" in first, first
    first = first["result"]
    assert len(first["runs"]) == 1
    assert first["runs"][0]["profile"] == "ops"
    assert first["runs"][0]["outcome"] == "succeeded"
    assert first["next_cursor"]
    second = summary(cursor=first["next_cursor"])["result"]
    third = summary(cursor=second["next_cursor"])["result"]
    runs = first["runs"] + second["runs"] + third["runs"]
    assert {(r["profile"], r["occurrence_id"]) for r in runs} == {
        ("ops", "execution:a"), ("default", "execution:a"), ("default", "execution:b")}
    assert all(r["retry_of"] is None and r["delivery_summary"] is None for r in runs)
    assert first["counts"] == second["counts"] == third["counts"]
    assert third["next_cursor"] is None
    assert "private error body" not in json.dumps(runs)


def test_cursor_binds_scope_and_rejects_changed_snapshot(homes):
    with ledger(homes[0]) as conn:
        attempt(conn, "a")
        attempt(conn, "b")
    response = summary()
    assert "result" in response, response
    cursor = response["result"]["next_cursor"]
    assert summary(cursor=cursor, timezone="America/Montreal").get("error", {}).get("code") == 4002
    assert summary(cursor=cursor, profiles=["ops"]).get("error", {}).get("code") == 4002
    with ledger(homes[0]) as conn:
        attempt(conn, "c")
    assert summary(cursor=cursor).get("error", {}).get("code") == 4002


@pytest.mark.parametrize("extra", [{"timezone": "No/SuchZone"}, {"end": 0}, {"start": True},
                                    {"end": 9999999999}, {"limit": True}, {"limit": 101}])
def test_summary_rejects_invalid_window_timezone_and_limit(homes, extra):
    response = summary(**extra)
    assert response.get("error", {}).get("code") == 4002, response


def test_known_turn_start_is_not_chat_creation_time(homes):
    _, named, _ = homes
    server._sessions["runtime"] = {"profile_home": str(named), "session_key": "same-id", "running": True,
                                   "inflight_turn": {"started_at": 123, "user": "not a task title"}}
    response = rpc("team.activity", profiles=["ops"])
    assert "result" in response, response
    current = response["result"]["agents"][0]["current"]
    assert current["started_at"] == 123
    assert "not a task title" not in str(response)


def test_epoch_zero_occurrence_stays_in_its_scheduled_window(homes):
    with ledger(homes[0]) as conn:
        attempt(conn, "zero", scheduled="1970-01-01T00:00:00+00:00",
                claimed="1970-01-01T01:00:00+00:00")
    response = summary(end=10)
    assert "result" in response, response
    assert [r["occurrence_id"] for r in response["result"]["runs"]] == ["execution:zero"]


def test_live_cron_ownership_is_positive_without_an_ordinary_chat(homes):
    from gateway.status import get_process_start_time
    with ledger(homes[1]) as conn:
        attempt(conn, "live", status="running")
        conn.execute("UPDATE executions SET process_started_at=?", (get_process_start_time(os.getpid()),))
        conn.commit()
    response = rpc("team.activity", profiles=["ops"])
    assert "result" in response, response
    current = response["result"]["agents"][0]
    assert current["working"] is True
    assert current["current"]["kind"] == "cron" and current["current"]["open_chat"] is None
    with ledger(homes[1]) as conn:
        conn.execute("UPDATE executions SET process_started_at=1")
        conn.commit()
    assert rpc("team.activity", profiles=["ops"])["result"]["agents"][0]["working"] is None


def test_desktop_delegate_and_canonical_continuation_never_open_as_ordinary(homes):
    _, named, _ = homes
    with SessionDB(db_path=named / "state.db") as db:
        db.create_session("child", "desktop", model_config={"_delegate_from": "same-id"})
        db._conn.execute("UPDATE sessions SET title='Bot Chat',end_reason='compression',ended_at=1 WHERE id='same-id'")
        db.create_session("tip", "desktop", parent_session_id="same-id")
    for key in ("child", "tip"):
        server._sessions["runtime"] = {"profile_home": str(named), "session_key": key, "running": True}
        response = rpc("team.activity", profiles=["ops"])
        assert "result" in response, response
        assert response["result"]["agents"][0]["current"]["open_chat"] is None


def test_storage_reads_do_not_wait_on_state_writer_or_mutate_state(homes):
    _, named, _ = homes
    path = named / "state.db"
    with sqlite3.connect(path, timeout=0) as writer:
        writer.execute("BEGIN IMMEDIATE")
        response = rpc("team.activity", profiles=["ops"])
        assert "result" in response, response
        assert writer.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        writer.rollback()


@pytest.mark.parametrize("method", ["team.activity", "cron.summary"])
def test_named_launch_owner_admission_does_not_hydrate_root_secrets(homes, method, monkeypatch):
    from hermes_cli import env_loader, web_server_profiles
    from tui_gateway import launch_profile_policy
    root, named, _ = homes
    monkeypatch.setenv("HERMES_HOME", str(named))
    monkeypatch.setattr(server, "_hermes_home", named)
    monkeypatch.setattr(web_server_profiles, "get_process_hermes_home", lambda: named)
    effects = []
    monkeypatch.setattr(env_loader, "hydrate_profile_secret_sources", lambda home: effects.append(str(home)))
    monkeypatch.setattr(launch_profile_policy, "activate_multi_profile_hosting", lambda: effects.append("multiplex"))
    before = dict(os.environ)
    response = rpc(method, **({"profiles": ["ops"]} if method == "team.activity" else {
        "profiles": ["ops"], "timezone": "UTC", "start": 0, "end": 1000}))
    assert "result" in response, response
    assert effects == []
    assert dict(os.environ) == before
    from hermes_constants import get_hermes_home
    assert get_hermes_home() == named
    assert root != named


@pytest.mark.parametrize("source,hidden,expected_last,opens", [
    ("desktop", 1, None, False), (None, 0, 100, True), ("desktop", None, 100, True)])
def test_team_ordinary_chat_matches_hidden_and_null_source_listing(homes, source, hidden, expected_last, opens):
    _, named, _ = homes
    with sqlite3.connect(named / "state.db") as conn:
        if source is None:
            conn.execute("ALTER TABLE sessions RENAME COLUMN source TO original_source")
            conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT")
            conn.execute("UPDATE sessions SET source=original_source")
        if hidden is None:
            conn.execute("ALTER TABLE sessions RENAME COLUMN hidden TO original_hidden")
        else:
            conn.execute("UPDATE sessions SET source=?,hidden=? WHERE id='same-id'", (source, hidden))
    server._sessions["runtime"] = {"profile_home": str(named), "session_key": "same-id", "running": True}
    response = rpc("team.activity", profiles=["ops"])
    assert "result" in response, response
    agent = response["result"]["agents"][0]
    assert agent["last_human_chat_at"] == expected_last
    assert (agent["current"]["open_chat"] is not None) is opens
    if hidden is None:
        with sqlite3.connect(named / "state.db") as conn:
            assert "hidden" not in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}


@pytest.mark.parametrize("method", ["team.activity", "cron.summary"])
@pytest.mark.parametrize("error", [PermissionError, FileNotFoundError])
def test_profile_store_io_errors_are_not_owner_denial_or_unknown_profile(homes, method, error, monkeypatch):
    from tui_gateway import team_schedule_projection
    with ledger(homes[1]) as conn:
        attempt(conn, "stored")

    def unavailable(path):
        raise error("fixture storage failure")

    monkeypatch.setattr(team_schedule_projection, "_read_store", unavailable)
    response = rpc(method, **({"profiles": ["ops"]} if method == "team.activity" else {
        "profiles": ["ops"], "timezone": "UTC", "start": 0, "end": 1000}))
    assert response.get("error", {}).get("code") == 5037, response
    assert "fixture storage failure" not in str(response)


@pytest.mark.parametrize("method", ["team.activity", "cron.summary"])
def test_public_dispatch_moves_read_to_worker_pool(homes, method, monkeypatch):
    peer = homes[2]
    entered = threading.Event()
    release = threading.Event()
    original = server._methods.get(method)
    assert original is not None, "public method must be registered"

    def blocked(rid, params):
        entered.set()
        assert release.wait(10)
        return original(rid, params)

    monkeypatch.setitem(server._methods, method, blocked)
    params = {"profiles": ["ops"]}
    if method == "cron.summary":
        params.update(timezone="UTC", start=0, end=1000)
    try:
        assert server.dispatch({"id": "pooled", "method": method, "params": params}, peer) is None
        assert entered.wait(5)
    finally:
        release.set()
    assert peer.ready.wait(5)
    assert "result" in peer.frames[-1], peer.frames
