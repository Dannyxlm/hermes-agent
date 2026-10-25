"""Upgrade an actual predecessor-shaped database, not an empty new store."""

import sqlite3
import uuid

import pytest

from tui_gateway.mobile_push_store import PushStore


_LEGACY_SCHEMA = """
CREATE TABLE subscriptions (
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, installation_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 scope TEXT NOT NULL, surface TEXT NOT NULL, profile TEXT NOT NULL, session_id TEXT NOT NULL,
 kind TEXT NOT NULL, activity_id TEXT NOT NULL, run_id TEXT NOT NULL, token TEXT NOT NULL,
 environment TEXT NOT NULL, categories TEXT NOT NULL, expires_at REAL NOT NULL, version INTEGER NOT NULL,
 preview_enabled INTEGER NOT NULL DEFAULT 0, last_timestamp INTEGER NOT NULL DEFAULT 0,
 UNIQUE(principal, installation_id, connection_id, scope, kind, activity_id));
CREATE TABLE outbox (
 job_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 event_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL, urgent INTEGER NOT NULL,
 collapse_id TEXT NOT NULL, expires_at REAL NOT NULL, next_attempt REAL NOT NULL,
 category TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
 lease_until REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
 UNIQUE(subscription_id,event_id));
CREATE TABLE alert_policies (
 principal TEXT NOT NULL, installation_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 policy TEXT NOT NULL, token TEXT NOT NULL, environment TEXT NOT NULL, categories TEXT NOT NULL,
 preview_enabled INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL,
 PRIMARY KEY(principal, installation_id, connection_id));
CREATE TABLE activity_start_tokens (
 principal TEXT NOT NULL, installation_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 token TEXT NOT NULL, environment TEXT NOT NULL, expires_at REAL NOT NULL,
 PRIMARY KEY(principal, installation_id, connection_id));
"""


@pytest.mark.parametrize("kind", ["alert", "widget", "background", "activity", "activity_start"])
@pytest.mark.parametrize("same_expiry", [False, True])
def test_upgrade_keeps_latest_device_lease_and_cascades_old_jobs(tmp_path, kind, same_expiry):
    path = tmp_path / "legacy.sqlite"
    ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    old_id, new_id = str(uuid.uuid4()), str(uuid.uuid4())
    with sqlite3.connect(path) as db:
        db.executescript(_LEGACY_SCHEMA)
        for n, sid in enumerate((old_id, new_id)):
            expiry = 2000 if same_expiry else 2000 + n
            db.execute("""INSERT INTO subscriptions
                (id,principal,installation_id,connection_id,scope,surface,profile,session_id,kind,
                 activity_id,run_id,token,environment,categories,expires_at,version)
                VALUES (?,?,?,?,?,'native_session','ops','root',?,'','','ab','production','[]',?,1)""",
                (sid, f"owner-{n}", *ids, "fixture-scope", kind, expiry))
            db.execute("""INSERT INTO outbox
                (job_id,subscription_id,event_id,run_id,payload,urgent,collapse_id,expires_at,next_attempt)
                VALUES (?,?,?,'','{}',1,'',3000,1000)""", (str(uuid.uuid4()), sid, f"event-{n}"))
            db.execute("INSERT INTO alert_policies VALUES (?,?,?,'all','ab','production','[]',0,?)",
                       (f"owner-{n}", *ids, expiry))
            db.execute("INSERT INTO activity_start_tokens VALUES (?,?,?,'cd','production',?)",
                       (f"owner-{n}", *ids, expiry))
    for _ in range(2):
        store = PushStore(path, lambda: 1000)
        try:
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT id,principal FROM subscriptions").fetchall() == [(new_id, "owner-1")]
                assert db.execute("SELECT subscription_id FROM outbox").fetchall() == [(new_id,)]
                for table in ("alert_policies", "activity_start_tokens"):
                    assert db.execute(f"SELECT principal FROM {table}").fetchall() == [("owner-1",)]
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            store.close()


def test_upgrade_retires_single_stale_policy_before_it_can_reclaim_new_lease(tmp_path):
    from tui_gateway.mobile_push_payloads import Scope

    path = tmp_path / "legacy-policy.sqlite"
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    scope = Scope("native_session", "ops", "root")
    with sqlite3.connect(path) as db:
        db.executescript(_LEGACY_SCHEMA)
        db.execute("INSERT INTO alert_policies VALUES (?,?,?,'all',?,'production','[]',0,2000)",
                   ("old-owner", *ids.values(), "ab" * 32))
        db.execute("""INSERT INTO subscriptions
            (id,principal,installation_id,connection_id,scope,surface,profile,session_id,kind,
             activity_id,run_id,token,environment,categories,expires_at,version)
            VALUES (?,'current-owner',?, ?,?,'native_session','ops','root','alert','','',?,'production','[]',3000,1)""",
            (str(uuid.uuid4()), *ids.values(), scope.key, "ab" * 32))
    store = PushStore(path, lambda: 1000)
    try:
        assert store.policy("old-owner", **ids)["policy"] == "opened"
        store.start_run(scope, listed=True)
        assert store.unregister("old-owner", **ids) == 0
        assert store.unregister("current-owner", **ids) == 1
    finally:
        store.close()


def test_upgrade_removes_starts_queued_for_replaced_token_owner(tmp_path):
    path = tmp_path / "legacy-start.sqlite"
    ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    sid = str(uuid.uuid4())
    with sqlite3.connect(path) as db:
        db.executescript(_LEGACY_SCHEMA)
        db.execute("INSERT INTO activity_start_tokens VALUES ('current-owner',?,?,'ab','production',3000)", ids)
        db.execute("""INSERT INTO subscriptions
            (id,principal,installation_id,connection_id,scope,surface,profile,session_id,kind,
             activity_id,run_id,token,environment,categories,expires_at,version)
            VALUES (?,'old-owner',?,?,'fixture-scope','native_session','ops','root',
                    'activity_start','run','run','ab','production','[]',2000,1)""", (sid, *ids))
        db.execute("""INSERT INTO outbox
            (job_id,subscription_id,event_id,run_id,payload,urgent,collapse_id,expires_at,next_attempt)
            VALUES (?,?,'start','run','{}',1,'',2000,1000)""", (str(uuid.uuid4()), sid))
    for _ in range(2):
        store = PushStore(path, lambda: 1000)
        try:
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
                assert db.execute("SELECT principal FROM activity_start_tokens").fetchall() == [("current-owner",)]
        finally:
            store.close()
