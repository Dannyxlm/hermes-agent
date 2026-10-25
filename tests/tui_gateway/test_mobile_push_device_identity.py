"""A physical APNs destination survives authenticated-principal changes without twins."""

import sqlite3
import uuid

import pytest

from tests.tui_gateway.test_mobile_push import Sender
from tui_gateway import mobile_inbox_invalidation as hints
from tui_gateway.mobile_push import PushService, Scope


@pytest.fixture
def device(tmp_path):
    now = [1800000000.0]
    sender = Sender()
    service = PushService(tmp_path / "push.sqlite", sender, clock=lambda: now[0])
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    yield service, sender, now, ids, Scope("native_session", "ops", "root")
    service.close()


@pytest.mark.parametrize("kind", ["alert", "widget", "activity"])
def test_principal_flip_delivers_one_job_per_device_event(device, kind):
    service, sender, now, ids, scope = device
    run = service.start_run(scope)
    options = {"kind": kind}
    if kind == "activity":
        options.update(activity_id="fixture-activity", run_id=run["run_id"])
    for n, principal in enumerate(("old-owner", "current-owner")):
        if kind == "widget":
            options["read_token"] = f"{n + 1:064x}"
        service.register(principal, scope, **ids, token="ab" * 32, environment="production", **options)
        now[0] += 1
    service.record(scope, run["run_id"], "one-event", "waitingForApproval")
    now[0] += 12
    service.drain_once()
    assert [job["token"] for job in sender.jobs] == ["ab" * 32]
    assert service.unregister("old-owner", **ids) == 0
    assert service.unregister("current-owner", **ids) == 1


def test_principal_flip_replaces_background_lease(device):
    service, _sender, _now, ids, _scope = device
    for principal in ("old-owner", "current-owner"):
        hints.register(service.store, principal, **ids, profile="ops", device_token="ab" * 32,
                       environment="production")
    assert hints.schedule(service.store, {"ops"}) == 1
    assert service.unregister("old-owner", **ids) == 0
    assert service.unregister("current-owner", **ids) == 1


def test_policy_flip_cannot_rematerialize_old_principal(device):
    service, sender, now, ids, scope = device
    for principal in ("old-owner", "current-owner"):
        service.set_policy(principal, **ids, policy="all", token="ab" * 32, environment="production")
        now[0] += 1
    run = service.start_run(scope, listed=True)
    service.record(scope, run["run_id"], "one-event", "waitingForApproval")
    service.drain_once()
    assert [job["token"] for job in sender.jobs] == ["ab" * 32]
    assert service.store.policy("old-owner", **ids)["policy"] == "all"


def test_explicit_registration_keeps_another_humans_all_policy(device):
    service, sender, now, ids, scope = device
    service.set_policy("old-owner", **ids, policy="all", token="ab" * 32, environment="production")
    now[0] += 1
    service.register("current-owner", scope, **ids, token="ab" * 32, environment="production")
    run = service.start_run(scope, listed=False)
    service.record(scope, run["run_id"], "one-event", "waitingForApproval")
    service.drain_once()
    assert [job["principal"] for job in sender.jobs] == ["current-owner"]
    assert service.store.policy("old-owner", **ids)["policy"] == "all"


def test_policy_for_another_human_keeps_unlisted_explicit_lease(device):
    service, sender, now, ids, scope = device
    service.register("old-owner", scope, **ids, token="ab" * 32, environment="production")
    run = service.start_run(scope)
    now[0] += 1
    service.set_policy("current-owner", **ids, policy="all", token="ab" * 32, environment="production")
    service.record(scope, run["run_id"], "one-event", "waitingForApproval")
    service.drain_once()
    assert [job["principal"] for job in sender.jobs] == ["old-owner"]
    assert service.unregister("old-owner", **ids) == 1


def test_start_token_for_another_human_keeps_old_origin(device):
    service, sender, _now, ids, scope = device
    for principal in ("old-owner", "current-owner"):
        service.register_start_token(principal, **ids, token="ab" * 32, environment="production")
    service.store.set_origin(scope, "old-owner", **ids)
    service.start_run(scope, continuation=True)
    service.drain_once()
    assert [job["principal"] for job in sender.jobs] == ["old-owner"]
    sender.jobs.clear()
    service.store.set_origin(scope, "current-owner", **ids)
    service.start_run(scope, continuation=True)
    service.drain_once()
    assert [job["kind"] for job in sender.jobs] == ["activity_start"]


def test_another_humans_start_token_keeps_already_queued_start(device):
    service, sender, _now, ids, scope = device
    service.register_start_token("old-owner", **ids, token="ab" * 32, environment="production")
    service.store.set_origin(scope, "old-owner", **ids)
    service.start_run(scope, continuation=True)
    service.register_start_token("current-owner", **ids, token="ab" * 32, environment="production")
    service.drain_once()
    assert [job["principal"] for job in sender.jobs] == ["old-owner"]
    assert service.unregister("old-owner", **ids, kind="activity_start") == 1


def test_delivery_backstop_deduplicates_same_token_across_installations(device):
    service, sender, _now, ids, scope = device
    for principal in ("owner-a", "owner-b"):
        service.register(principal, scope, **{**ids, "installation_id": str(uuid.uuid4())},
                         token="ab" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "one-event", "waitingForApproval")
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    service.drain_once()
    assert [job["token"] for job in sender.jobs] == ["ab" * 32]


def test_replacement_revokes_old_refresh_presence_and_pending_delivery(device):
    service, sender, _now, ids, scope = device
    old = service.register("old-owner", scope, **ids, token="ab" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "old-event", "waitingForApproval")
    current = service.register("current-owner", scope, **ids, token="ab" * 32, environment="production",
                               preview_enabled=False, categories=["completion"])
    assert current["subscription_id"] != old["subscription_id"]
    assert service.refresh("old-owner", **ids, token="ab" * 32, environment="production",
                           accepts_scope=lambda _: True, preview_enabled=True)["updated"] == 0
    with pytest.raises(ValueError, match="registered destination required"):
        service.store.presence("old-owner", scope, **ids, foreground=True)
    assert service.unregister("old-owner", **ids, subscription_id=current["subscription_id"]) == 0
    assert service.drain_once() == 0
    service.record(scope, run["run_id"], "done", "complete")
    service.drain_once()
    assert [job["principal"] for job in sender.jobs] == ["current-owner"]


@pytest.mark.parametrize("change", ["token", "environment", "scope", "installation_id"])
def test_device_key_does_not_revoke_unrelated_registrations(device, change):
    service, _sender, _now, ids, scope = device
    options = {**ids, "token": "ab" * 32, "environment": "production"}
    service.store.register("old-owner", scope, **options)
    if change == "scope":
        scope = Scope(scope.surface, scope.profile, "another-root")
    elif change == "token":
        options[change] = "cd" * 32
    elif change == "environment":
        options[change] = "sandbox"
    else:
        options[change] = str(uuid.uuid4())
    service.store.register("current-owner", scope, **options)
    assert service.unregister("old-owner", **ids) == 1
    assert service.unregister("current-owner", **{k: options[k] for k in ids}) == 1


def test_token_rotation_reconciles_conflict_without_changing_owner(device):
    service, _sender, _now, ids, scope = device
    service.register("old-owner", scope, **ids, token="ab" * 32, environment="production")
    service.register("current-owner", scope, **ids, token="cd" * 32, environment="production")
    assert service.refresh("current-owner", **ids, token="ab" * 32, environment="production",
                           accepts_scope=lambda _: True)["updated"] == 1
    assert service.unregister("old-owner", **ids) == 0
    assert service.unregister("current-owner", **ids) == 1


def test_tokenless_widget_readers_do_not_prove_same_physical_device(device):
    service, _sender, _now, ids, scope = device
    for n, principal in enumerate(("old-owner", "current-owner")):
        service.register(principal, scope, **ids, token="", environment="production", kind="widget",
                         read_token=f"{n + 1:064x}")
    assert service.unregister("old-owner", **ids) == 1
    assert service.unregister("current-owner", **ids) == 1
