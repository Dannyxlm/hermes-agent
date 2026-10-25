"""Preserve one human's complete device enrollment across a principal derivation upgrade."""

import hashlib
import sqlite3
import uuid

import pytest

from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer, rpc, open_bot, scope as runtime_scope  # noqa: PLC0414
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service  # noqa: PLC0414
from tui_gateway import mobile_inbox_invalidation as hints
from tui_gateway.methods_browser_control import _principal_digest
from tui_gateway.mobile_push import Scope
from tui_gateway.mobile_push_store import PushStore


IDENTITY = {"provider": "test-auth", "user_id": "test-user"}
LEGACY = "principal:dashboard:" + hashlib.sha256(b"test-auth\0test-user").hexdigest()[:32]


def enroll(service, principal, ids, scope, home, credential="dd" * 32):
    service.register(principal, scope, **ids, token="aa" * 32, environment="production")
    service.set_policy(principal, **ids, policy="all", token="aa" * 32, environment="production")
    service.register_start_token(principal, **ids, token="bb" * 32, environment="production")
    service.store.set_origin(scope, principal, **ids)
    service.store.presence(principal, scope, **ids, foreground=True)
    service.register(principal, scope, **ids, kind="widget", token="cc" * 32,
                     read_token=credential, environment="production")
    service.store.register_widget_inbox(principal, **ids, profile="ops", home=home, token=credential)
    hints.register(service.store, principal, **ids, profile="ops", device_token="aa" * 32, environment="production")


def test_startup_preserves_all_chats_then_authenticated_rpc_adopts_it(push_service, peer, mobile_home):
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    current = _principal_digest(IDENTITY)
    scope = Scope("native_session", "ops", "opened")
    enroll(push_service, LEGACY, ids, scope, mobile_home / "profiles" / "ops")
    push_service.register(current, scope, **ids, token="aa" * 32, environment="production")
    reopened = PushStore(push_service.store.path, push_service._clock)
    reopened.close()
    before = Scope("native_session", "ops", "new-before-rpc")
    run = push_service.start_run(before, listed=True)
    push_service.record(before, run["run_id"], "ask", "waitingForApproval")
    first = push_service.store.claim()
    assert first is not None and first["kind"] == "alert" and first["principal"] == LEGACY
    assert push_service.store.claim() is None
    peer.auth_identity = IDENTITY
    assert rpc("mobile.session_push.policy", **ids)["result"]["policy"] == "all"
    after = Scope("native_session", "ops", "new-after-rpc")
    run = push_service.start_run(after, listed=True)
    push_service.record(after, run["run_id"], "ask", "waitingForApproval")
    second = push_service.store.claim()
    assert second is not None and second["kind"] == "alert" and second["principal"] == current
    assert push_service.store.claim() is None
    reader = push_service.store.widget_inbox_grant("dd" * 32)
    assert reader["principal"] == current
    assert push_service.store.origin(scope)["principal"] == current
    assert rpc("mobile.push.unregister", **ids)["result"]["removed"] > 0
    with sqlite3.connect(push_service.store.path) as db:
        for table in ("subscriptions", "alert_policies", "activity_start_tokens", "human_origins",
                      "widget_readers", "widget_inbox_grants", "mobile_presence"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE installation_id=?", (ids["installation_id"],)).fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("issuer", ["https://issuer-one.invalid", "https://issuer-two.invalid"])
def test_federated_identity_never_adopts_legacy_rows(push_service, peer, mobile_home, issuer):
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    enroll(push_service, LEGACY, ids, Scope("native_session", "ops", "opened"), mobile_home)
    peer.auth_identity = {**IDENTITY, "issuer": issuer}
    assert rpc("mobile.session_push.policy", **ids)["result"]["policy"] == "opened"
    assert rpc("mobile.push.unregister", **ids)["result"]["removed"] == 0
    assert push_service.store.policy(LEGACY, **ids)["policy"] == "all"


@pytest.mark.parametrize("method,extra", [
    ("mobile.push.refresh", {"environment": "production", "device_token": "aa" * 32}),
    ("mobile.inbox_push.register", {"profile": "ops", "environment": "production", "device_token": "aa" * 32}),
    ("mobile.session_activity.start_token.register", {"environment": "production", "start_token": "bb" * 32}),
    ("mobile.widget.inbox.register", {"profile": "ops", "read_token": "dd" * 32}),
    ("mobile.push.register", {"environment": "production", "device_token": "aa" * 32}),
    ("mobile.widget.register", {"profile": "ops", "stored_session_id": "adoption-widget-root", "environment": "production",
                                "widget_token": "cc" * 32, "read_token": "dd" * 32}),
    ("mobile.activity.refresh", {"environment": "production", "activity_token": "bb" * 32,
                               "activity_id": "fixture", "run_id": "unavailable"}),
    ("mobile.session_push.policy", {"policy": "all", "environment": "production", "device_token": "aa" * 32}),
])
def test_registration_paths_adopt_complete_installation(push_service, peer, mobile_home, method, extra):
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    enroll(push_service, LEGACY, ids, Scope("native_session", "ops", "opened"), mobile_home)
    peer.auth_identity = IDENTITY
    if method == "mobile.push.register":
        extra = {**extra, **runtime_scope(open_bot())}
    if method == "mobile.widget.register":
        from hermes_state import SessionDB
        with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
            db.create_session("adoption-widget-root", "desktop")
    result = rpc(method, **ids, **extra)
    assert "result" in result
    with sqlite3.connect(push_service.store.path) as db:
        for table in ("subscriptions", "alert_policies", "activity_start_tokens", "human_origins",
                      "widget_readers", "widget_inbox_grants", "mobile_presence"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE principal=? AND installation_id=?",
                              (LEGACY, ids["installation_id"])).fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("legacy_newer", [False, True])
def test_adoption_conflicts_keep_newest_reader_grant_and_lease(push_service, peer, mobile_home, legacy_newer):
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    scope = Scope("native_session", "ops", "opened")
    current = _principal_digest(IDENTITY)
    now = [2000 if legacy_newer else 1000]
    push_service.store.clock = lambda: now[0]
    enroll(push_service, LEGACY, ids, scope, mobile_home / "legacy-grant")
    now[0] = 1000 if legacy_newer else 2000
    enroll(push_service, current, ids, scope, mobile_home / "current-grant", credential="ee" * 32)
    # Different APNs tokens do not twin-match, but their authenticated principal keys will conflict.
    now[0] = 2000 if legacy_newer else 1000
    push_service.register(LEGACY, scope, **ids, token="ab" * 32, environment="production")
    push_service.store.refresh(LEGACY, **ids, token="ab" * 32, environment="production",
                               accepts_scope=lambda _: True)
    peer.auth_identity = IDENTITY
    assert rpc("mobile.session_push.policy", **ids)["result"]["policy"] == "all"
    winner, loser = ("dd" * 32, "ee" * 32) if legacy_newer else ("ee" * 32, "dd" * 32)
    grant = push_service.store.widget_inbox_grant(winner)
    assert grant["principal"] == current
    assert grant["home"].endswith("legacy-grant" if legacy_newer else "current-grant")
    assert push_service.store.widget_reader(loser) is None
    # Retrying the RPC cannot rotate the surviving reader or its exact grant.
    assert rpc("mobile.session_push.policy", **ids)["result"]["policy"] == "all"
    assert push_service.store.widget_inbox_grant(winner)["grant_id"] == grant["grant_id"]
    with sqlite3.connect(push_service.store.path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT COUNT(*) FROM subscriptions WHERE principal=?", (LEGACY,)).fetchone()[0] == 0


def test_logout_adopts_even_when_delivery_is_disabled(push_service, peer, mobile_home, monkeypatch):
    from tui_gateway import mobile_push, server

    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    enroll(push_service, LEGACY, ids, Scope("native_session", "ops", "opened"), mobile_home)
    peer.auth_identity = IDENTITY
    monkeypatch.setattr(server, "_mobile_push_service", lambda: None)
    monkeypatch.setattr(mobile_push, "service_for_home", lambda _: None)
    assert rpc("mobile.push.unregister", **ids)["result"]["removed"] > 0
    with sqlite3.connect(push_service.store.path) as db:
        for table in ("subscriptions", "alert_policies", "activity_start_tokens", "human_origins",
                      "widget_readers", "widget_inbox_grants", "mobile_presence"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_adoption_keeps_queued_start_and_its_receipt(push_service, peer, mobile_home):
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    scope = Scope("native_session", "ops", "opened")
    enroll(push_service, LEGACY, ids, scope, mobile_home)
    push_service.start_run(scope, continuation=True)
    peer.auth_identity = IDENTITY
    assert rpc("mobile.session_push.policy", **ids)["result"]["policy"] == "all"
    start = push_service.store.claim()
    assert start is not None and start["kind"] == "activity_start"
    assert start["principal"] == _principal_digest(IDENTITY)
    assert push_service.store.claim() is None


def test_other_installation_and_human_are_not_adopted(push_service, peer):
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    other = {**ids, "installation_id": str(uuid.uuid4())}
    scope = Scope("native_session", "ops", "opened")
    push_service.register(LEGACY, scope, **other, token="aa" * 32, environment="production")
    push_service.register("another-human", scope, **ids, token="aa" * 32, environment="production")
    peer.auth_identity = IDENTITY
    assert rpc("mobile.session_push.policy", **ids)["result"]["policy"] == "opened"
    assert push_service.unregister(LEGACY, **other) == 1
    assert push_service.unregister("another-human", **ids) == 1


def test_losing_connection_reads_and_refreshes_one_device_enrollment(push_service):
    a = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    b = {**a, "connection_id": str(uuid.uuid4())}
    scope = Scope("native_session", "ops", "opened")
    for ids in (a, b):
        push_service.register("owner", scope, **ids, token="aa" * 32, environment="production")
        push_service.set_policy("owner", **ids, policy="all", token="aa" * 32, environment="production")
    assert push_service.store.policy("owner", **a)["policy"] == "all"
    assert push_service.refresh("owner", **a, token="aa" * 32, environment="production",
                                accepts_scope=lambda _: True)["updated"] == 1
    with sqlite3.connect(push_service.store.path) as db:
        assert db.execute("SELECT connection_id FROM subscriptions").fetchall() == [(b["connection_id"],)]
        assert db.execute("SELECT connection_id FROM alert_policies").fetchall() == [(b["connection_id"],)]
    push_service.set_policy("owner", **a, policy="opened", environment="production")
    assert push_service.store.policy("owner", **a)["policy"] == "opened"
    assert push_service.store.policy("owner", **b)["policy"] == "opened"
    assert push_service.unregister("owner", **a) == 1
    assert push_service.unregister("owner", **b) == 0
