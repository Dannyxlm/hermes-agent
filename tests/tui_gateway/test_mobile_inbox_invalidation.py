"""Inbox-changed silent pushes (Hermex R9 U12, KTD9): content-free hints, the 20-minute cap with a
durable trailing flag, the qualifying triggers, and revocation on logout.

Real push store, real profile stores, the real watcher read and the real push worker pass; only the
APNs sender and the store clock are fakes.
"""

import json
import time
import uuid

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc  # noqa: F401 - fixtures
from tests.tui_gateway.test_mobile_push import Sender
from tui_gateway import mobile_inbox_invalidation as hints
from tui_gateway import server, server_requests, session_change_cursor
from tui_gateway.mobile_push import PushService
from tui_gateway.mobile_push_payloads import Scope

PRINCIPAL = "owner-digest"


@pytest.fixture
def push(mobile_home, monkeypatch):
    hints.reset_for_tests()
    session_change_cursor.reset_for_tests()
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    ops = mobile_home / "profiles" / "ops"
    with SessionDB(db_path=ops / "state.db") as db:
        db.create_session("same", "desktop")
        db.set_session_title("same", "Private title sentinel")
        db.create_session("tg", "telegram")
    now = [1800000000.0]
    service = PushService(mobile_home / "mobile-push" / "outbox.sqlite3", Sender(), clock=lambda: now[0])
    monkeypatch.setattr(server, "_mobile_push_service", lambda: service)
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    lease = hints.register(service.store, PRINCIPAL, **ids, profile="ops", device_token="ab" * 32,
                           environment="production")
    server._session_db_content_sig(ops / "state.db")  # the watcher's first read seeds the journal
    service.drain_once()  # the first worker pass seeds the run watermark
    yield service, now, ids, lease, ops / "state.db"
    service.close()
    hints.reset_for_tests()
    session_change_cursor.reset_for_tests()


def _write(db_path, fn):
    time.sleep(0.01)
    with SessionDB(db_path=db_path) as db:
        fn(db)
    server._session_db_content_sig(db_path)  # what the watcher's next pass does


def _pass(service, now, advance=0.0):
    now[0] += advance
    service.drain_once()
    return len(service.sender.jobs)


def test_new_reply_schedules_one_hint_and_tool_events_none(push):
    service, now, _ids, lease, db_path = push
    for n in range(10):
        _write(db_path, lambda db, n=n: (
            db.append_message("same", "assistant", f"calling {n}", finish_reason="tool_calls"),
            db.append_message("same", "tool", f"output {n}", tool_name="terminal")))
        assert _pass(service, now, 1.0) == 0

    _write(db_path, lambda db: db.append_message("same", "assistant", "final reply sentinel", finish_reason="stop"))
    assert _pass(service, now, 1.0) == 1
    [job] = service.sender.jobs
    assert job["kind"] == "background" and job["urgent"] == 0 and job["token"] == "ab" * 32
    # Content-free: nothing beyond the opaque scope token.
    assert job["payload"] == {"aps": {"content-available": 1},
                              "hermex.inbox": {"version": 1, "scope": lease["scope_token"]}}
    wire = json.dumps(job["payload"])
    for private in ("same", "ops", "Private title sentinel", "final reply sentinel"):
        assert private not in wire


def test_messaging_replies_are_not_inbox_evidence(push):
    service, now, *_rest, db_path = push
    _write(db_path, lambda db: db.append_message("tg", "assistant", "telegram reply", finish_reason="stop"))
    assert _pass(service, now, 1.0) == 0


def test_three_changes_in_window_send_one_push_and_one_trailing_push(push):
    service, now, *_rest = push
    for minute in (0, 1, 2):
        hints.note_profile("ops")
        assert _pass(service, now, 60.0 if minute else 0.0) == 1
    assert _pass(service, now, hints.MIN_INTERVAL_S - 121) == 1  # still inside the window
    assert _pass(service, now, 1.0) == 2  # the window opens: exactly one trailing hint
    assert _pass(service, now, hints.MIN_INTERVAL_S) == 2  # trailing flag cleared, nothing new


def test_trailing_flag_is_durable_across_a_worker_restart(push):
    service, now, *_rest = push
    hints.note_profile("ops")
    _pass(service, now)
    hints.note_profile("ops")
    _pass(service, now, 30.0)  # dirty, not sent
    restarted = PushService(service.store.path, Sender(), clock=lambda: now[0])
    hints.reset_for_tests()  # a new process: in-memory intent is gone, the flag is not
    now[0] += hints.MIN_INTERVAL_S
    restarted.drain_once()
    assert len(restarted.sender.jobs) == 1
    restarted.close()


def test_projection_changes_qualify_without_a_reply(push):
    service, now, _ids, _lease, db_path = push
    _write(db_path, lambda db: db.set_session_archived("same", True))  # Done
    assert _pass(service, now, 1.0) == 1
    _write(db_path, lambda db: db.set_session_title("same", "Renamed"))
    assert _pass(service, now, hints.MIN_INTERVAL_S) == 2


def test_pending_approval_added_and_answered_each_count(push, mobile_home):
    service, now, *_rest = push
    server._sessions["ops-runtime"] = {"session_key": "same", "profile_home": str(mobile_home / "profiles" / "ops")}
    settle = server_requests.send_async("approval", "ops-runtime", {"request_id": "r1", "choices": ["once", "deny"]},
                                        lambda result: None)
    assert _pass(service, now, 1.0) == 1
    settle("answered")
    assert _pass(service, now, 1.0) == 1  # inside the window: marked dirty
    assert _pass(service, now, hints.MIN_INTERVAL_S) == 2


def test_run_start_and_end_count_but_progress_does_not(push):
    service, now, *_rest = push
    scope = Scope("native_session", "ops", "same")
    now[0] += 1.0
    run = service.start_run(scope)
    assert _pass(service, now, 1.0) == 1
    now[0] += hints.MIN_INTERVAL_S
    service.record(scope, run["run_id"], "tool-1", "usingTool")
    assert _pass(service, now, 1.0) == 1  # progress is not an Inbox change
    service.record(scope, run["run_id"], "done", "complete")
    assert _pass(service, now, 1.0) == 2


def test_other_profiles_and_unleased_profiles_send_nothing(push):
    service, now, *_rest = push
    hints.note_profile("default")
    assert _pass(service, now, 1.0) == 0


def test_logout_revokes_the_lease_and_queued_hints(push):
    service, now, ids, _lease, _db_path = push
    hints.note_profile("ops")
    hints.process(service.store)  # queued, not yet claimed
    assert service.store.unregister(PRINCIPAL, **ids) >= 1  # mobile.push.unregister (logout)
    assert _pass(service, now, 1.0) == 0
    hints.note_profile("ops")
    assert _pass(service, now, hints.MIN_INTERVAL_S) == 0
    with service.store._lock:
        assert service.store._db.execute("SELECT COUNT(*) FROM inbox_hints").fetchone()[0] == 0


def test_invalid_device_token_drops_the_lease(push):
    from tui_gateway.mobile_push_provider import DeliveryResult
    service, now, *_rest = push
    service.sender.result = DeliveryResult("invalid", "token_invalid")
    hints.note_profile("ops")
    assert _pass(service, now, 1.0) == 1
    hints.note_profile("ops")
    assert _pass(service, now, hints.MIN_INTERVAL_S) == 1


def test_provider_sends_a_background_push(tmp_path):
    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from tui_gateway.mobile_push_provider import APNsProvider
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "test.p8"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    path.chmod(0o600)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})
    provider = APNsProvider({"team_id": "8AA929B9Q5", "key_id": "TESTKEY123", "private_key_path": str(path)},
                            client=httpx.Client(transport=httpx.MockTransport(handler)))
    payload = {"aps": {"content-available": 1}, "hermex.inbox": {"version": 1, "scope": "t"}}
    job = {"kind": "background", "environment": "production", "job_id": str(uuid.uuid4()), "expires_at": 1800000200,
           "urgent": 0, "collapse_id": "c", "token": "aa" * 32, "payload": payload}
    assert provider.send(job).outcome == "accepted"
    headers = requests[0].headers
    assert headers["apns-push-type"] == "background"
    assert headers["apns-priority"] == "5"
    assert headers["apns-topic"] == "co.cloudseed.hermex.ava"
    assert json.loads(requests[0].content) == payload
    provider.close()


def test_rpc_register_is_owner_scoped_idempotent_and_revocable(push, peer):
    service, _now, *_rest = push
    params = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4()), "profile": "ops",
              "device_token": "cd" * 32, "environment": "production"}
    assert rpc("mobile.inbox_push.register", **params)["error"]["code"] == 4403
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    assert rpc("mobile.inbox_push.register", **{**params, "profile": "nobody"})["error"]["code"] == 4400
    first = rpc("mobile.inbox_push.register", **params)["result"]
    assert first["min_interval_s"] == 1200 and len(first["scope_token"]) == 32
    again = rpc("mobile.inbox_push.register", **params)["result"]
    assert (again["subscription_id"], again["scope_token"]) == (first["subscription_id"], first["scope_token"])
    with service.store._lock:
        version = service.store._db.execute("SELECT version FROM subscriptions WHERE id=?",
                                            (first["subscription_id"],)).fetchone()[0]
    assert version == 1  # same token: renewal only, never a rotation
    ids = {key: params[key] for key in ("installation_id", "connection_id")}
    assert rpc("mobile.inbox_push.unregister", **ids)["result"]["removed"] == 1
    assert "inbox_silent_push" in rpc("mobile.capabilities")["result"]["features"]


def test_worker_notices_replies_with_no_watcher_or_client(push, monkeypatch):
    """After a restart nothing may have served the profile or connected a client: the push
    worker reads the leased store itself."""
    service, now, _ids, _lease, db_path = push
    session_change_cursor.reset_for_tests()
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    hints.reset_for_tests()
    assert _pass(service, now, 1.0) == 0  # seeds the journal and the run watermark silently
    with SessionDB(db_path=db_path) as db:  # no watcher pass, no list read
        db.append_message("same", "assistant", "written by another process", finish_reason="stop")
    assert _pass(service, now, 1.0) == 1


def test_scheduling_failure_keeps_the_intent(push, monkeypatch):
    service, now, *_rest = push
    real = hints.schedule
    calls = []

    def flaky(store, profiles, now=None):
        calls.append(set(profiles))
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real(store, profiles, now)
    monkeypatch.setattr(hints, "schedule", flaky)
    hints.note_profile("ops")
    assert _pass(service, now, 1.0) == 0
    assert _pass(service, now, 1.0) == 1
    assert calls == [{"ops"}, {"ops"}]


def test_token_rotation_during_an_in_flight_send_keeps_the_lease(push):
    from tui_gateway.mobile_push_provider import DeliveryResult
    service, now, ids, lease, _db_path = push
    hints.note_profile("ops")
    hints.process(service.store)
    job = service.store.claim()  # an APNs send is in flight with the old token
    rotated = hints.register(service.store, PRINCIPAL, **ids, profile="ops", device_token="ef" * 32,
                             environment="production")
    assert (rotated["subscription_id"], rotated["scope_token"]) == (lease["subscription_id"], lease["scope_token"])
    service.store.finish(job, DeliveryResult("invalid", "token_invalid"))  # Apple rejects the OLD token
    now[0] += 60  # past the retry backoff, well inside the hint's one-hour expiry
    service.drain_once()  # the retry goes out with the rotated token; the lease survives
    assert [j["token"] for j in service.sender.jobs] == ["ef" * 32]
