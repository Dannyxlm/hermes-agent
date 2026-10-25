"""The device invariant must preserve ignored inserts and one-time startup semantics."""

import sqlite3
import uuid

from tests.tui_gateway.test_mobile_push import delivery as delivery  # noqa: PLC0414
from tui_gateway.mobile_push_store import PushStore


def test_ignored_principal_key_insert_keeps_existing_start_and_leased_job(delivery):
    service, _sender, _now, ids, scope = delivery
    service.register_start_token("p", **ids, token="aa" * 32, environment="production")
    service.store.set_origin(scope, "p", **ids)
    service.start_run(scope, continuation=True)
    job = service.store.claim()
    assert job is not None and job["kind"] == "activity_start"
    with service.store.transaction() as db:
        columns = [row["name"] for row in db.execute("PRAGMA table_info(subscriptions)") if row["name"] != "id"]
        names = ",".join(columns)
        count = db.execute(f"INSERT OR IGNORE INTO subscriptions (id,{names}) SELECT ?,{names} FROM subscriptions WHERE id=?",
                           (str(uuid.uuid4()), job["subscription_id"])).rowcount
        assert count == 0
        assert db.execute("SELECT id FROM subscriptions").fetchall()[0][0] == job["subscription_id"]
        assert db.execute("SELECT lease_until FROM outbox WHERE job_id=?", (job["job_id"],)).fetchone()[0] > 0


def test_startup_does_not_repeat_authenticated_owner_cleanup(delivery):
    service, _sender, now, ids, scope = delivery
    service.register_start_token("current", **ids, token="aa" * 32, environment="production")
    with service.store.transaction() as db:
        db.execute("""INSERT INTO subscriptions
            (id,principal,installation_id,connection_id,scope,surface,profile,session_id,kind,
             activity_id,run_id,token,environment,categories,expires_at,version)
            VALUES (?,'legacy',?,?,?,'native','ops','root','activity_start','run','run',?,'production','[]',?,1)""",
            (str(uuid.uuid4()), *ids.values(), scope.key, "aa" * 32, now[0] + 1000))
    reopened = PushStore(service.store.path, lambda: now[0])
    reopened.close()
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1
