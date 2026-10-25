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
    assert service.store.policy("old-owner", **ids)["policy"] == "opened"


def test_start_token_flip_revokes_old_origin(device):
    service, sender, _now, ids, scope = device
    for principal in ("old-owner", "current-owner"):
        service.register_start_token(principal, **ids, token="ab" * 32, environment="production")
    service.store.set_origin(scope, "old-owner", **ids)
    service.start_run(scope)
    service.drain_once()
    assert sender.jobs == []


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
