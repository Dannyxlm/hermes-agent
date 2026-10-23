"""Exact-session wakes retain their target while execution belongs to the canonical owner."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cron import bot_chat_legacy as pending, jobs, scheduler_delivery, session_delivery
from hermes_constants import get_hermes_home
from hermes_state import SessionDB


@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with jobs.use_cron_store(home / "cron"):
        db = SessionDB(db_path=home / "state.db")
        db.create_session(session_id="release-chat", source="desktop")
        db.set_session_title("release-chat", "Build review")
        db.append_message("release-chat", "user", "Remember toy-marker-42")
        db.append_message("release-chat", "assistant", "Armed toy-marker-42")
        yield home, db
        db.close()


@pytest.fixture
def native_owner(owner_home, monkeypatch):
    """Exercise the real pending journal and native RPC boundary, never a live gateway."""
    from hermes_cli import gateway_client, gateway_runtime
    from tools import bot_live_delivery
    import subprocess

    home, _db = owner_home
    endpoint = object()
    state = SimpleNamespace(ready=True, calls=[], admissions=[], status="queued", outcome=None,
                            drop_answer=False, target=None, receipt=None)

    def discover(target_home, *, timeout):
        assert target_home == home
        return SimpleNamespace(state="ready" if state.ready else "missing",
                               endpoint=endpoint if state.ready else None)

    class Client:
        async def rpc(self, method, **params):
            state.calls.append((method, params))
            if method == "session.resume":
                return {"session_id": state.target or params["session_id"]}
            if method == "prompt.submit":
                key = params["submission_id"].removeprefix("cron-session:")
                record = pending.read_pending(key)
                assert record["status"] == "claimed"
                assert record["home"] == str(home)
                assert params["queued"] is True and params["finite"] is True
                assert "unattended" not in params
                state.admissions.append(params)
                state.receipt = {"admission_id": "wake-" + key, "status": state.status,
                                 "outcome": state.outcome, "ref": {"session_id": params["session_id"]}}
                if state.drop_answer:
                    raise ConnectionError("answer lost after possible admission")
                return state.receipt
            assert method == "prompt.receipt"
            assert params["admission_id"] == state.receipt["admission_id"]
            return {**state.receipt, "status": state.status, "outcome": state.outcome}

    expected_endpoint = endpoint

    @asynccontextmanager
    async def connect(*, endpoint):
        assert endpoint is expected_endpoint
        assert get_hermes_home() == home
        yield Client()

    def no_spawn(*args, **kwargs):
        pytest.fail("scheduled wakes must not spawn a gateway or local inference")

    monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint", discover)
    monkeypatch.setattr(gateway_runtime, "ensure_gateway_runtime", no_spawn)
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(bot_live_delivery, "read_delivery_result", lambda *a: None)
    return state


def receipt_key(job):
    return job["_bot_chat_delivery_receipts"]["session:release-chat"]["delivery_id"]


def test_delivery_validation_is_profile_local_and_atomic(owner_home, tmp_path):
    foreign = SessionDB(db_path=tmp_path / "foreign" / "state.db")
    foreign.create_session(session_id="foreign-chat", source="desktop")
    foreign.close()
    for target in ("session:foreign-chat", "session:missing", "session:", "session:../escape"):
        with pytest.raises(ValueError, match="session:<id>.*same profile"):
            jobs.create_job("Wake", "5m", deliver=target)
    assert jobs.list_jobs(include_disabled=True) == []
    job = jobs.create_job("Wake", "5m", deliver="session:release-chat")
    for field in ("deliver", "failure_deliver"):
        with pytest.raises(ValueError, match="session:<id>.*same profile"):
            jobs.update_job(job["id"], {field: "session:foreign-chat"})
    assert jobs.get_job(job["id"])["deliver"] == "session:release-chat"
    assert "failure_deliver" not in jobs.get_job(job["id"])


def test_owner_admits_each_execution_once_and_retains_fifo_intent(owner_home, native_owner, monkeypatch):
    from gateway import config
    monkeypatch.setattr(scheduler_delivery._sched, "load_config", dict)
    monkeypatch.setattr(config, "load_gateway_config", lambda: None)
    for run, payload in (("first", "Gate ready"), ("second", "Review complete")):
        job = jobs.create_job("Wake", "5m", name="Owner wake", deliver="session:release-chat")
        job["execution_id"] = run
        assert scheduler_delivery._deliver_result(job, payload) is None
        assert jobs.get_job(job["id"])["last_delivery_queued"]["session:release-chat"]["status"] == "queued"
        assert scheduler_delivery._deliver_result(job, payload) is None
    assert [item["text"] for item in native_owner.admissions] == [
        '[Scheduled wake from cron job "Owner wake" (first), not the user.]\n\nGate ready',
        '[Scheduled wake from cron job "Owner wake" (second), not the user.]\n\nReview complete',
    ]
    assert all(item["session_id"] == "release-chat" for item in native_owner.admissions)
    assert owner_home[1].get_session_by_title("Bot Chat") is None


def test_absent_owner_parks_then_admits_without_a_second_writer(owner_home, native_owner):
    native_owner.ready = False
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    key = receipt_key(job)
    pending.drain_legacy_pending()
    assert pending.read_pending(key)["status"] == "queued"
    assert native_owner.calls == []
    native_owner.ready = True
    pending.drain_legacy_pending()
    pending.drain_legacy_pending()
    assert len(native_owner.admissions) == 1
    assert pending.read_pending(key)["status"] == "transferred"


@pytest.mark.parametrize("status,outcome,expected", [
    ("terminal", "completed", "settled"), ("terminal", "failed", "failed"),
    ("unknown", None, "ambiguous"),
])
def test_admitted_receipts_are_observed_not_resubmitted(owner_home, native_owner, status, outcome, expected):
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    native_owner.status, native_owner.outcome = status, outcome
    pending.drain_legacy_pending()
    assert pending.read_pending(receipt_key(job))["status"] == expected
    result = session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    assert result is None if expected == "settled" else expected in result
    assert len(native_owner.admissions) == 1


def test_lost_submit_answer_is_ambiguous_and_never_replayed(owner_home, native_owner):
    native_owner.drop_answer = True
    job = {"id": "toy", "execution_id": "first"}
    assert "ambiguous" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    pending.drain_legacy_pending()
    assert "ambiguous" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    assert len(native_owner.admissions) == 1


def test_failure_suppression_is_durable_and_results_still_wake(owner_home, native_owner):
    home, _db = owner_home
    (home / "config.yaml").write_text("display: {suppress_warning_notifications: true}\n")
    job = {"id": "toy", "execution_id": "first"}
    assert session_delivery.deliver_to_session(job, "Failed", "release-chat", for_failure=True) is None
    assert pending.read_pending(receipt_key(job))["status"] == "suppressed"
    (home / "config.yaml").write_text("display: {suppress_warning_notifications: false}\n")
    assert session_delivery.deliver_to_session(job, "Failed", "release-chat", for_failure=True) is None
    assert native_owner.admissions == []
    assert "queued" in session_delivery.deliver_to_session(job, "Result", "release-chat")
    assert len(native_owner.admissions) == 1


def test_pending_payload_is_redacted_before_persistence(owner_home, native_owner):
    native_owner.ready = False
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "OPENAI_API_KEY=toy-private-value", "release-chat")
    assert pending.read_pending(receipt_key(job))["content"] == "OPENAI_API_KEY=***"


def test_compressed_conversation_targets_its_tip_not_a_bot_chat(owner_home, native_owner):
    _home, db = owner_home
    db.end_session("release-chat", end_reason="compression")
    db.create_session("release-tip", source="desktop", parent_session_id="release-chat")
    db.create_session("unrelated-chat", source="desktop")
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    assert native_owner.admissions[0]["session_id"] == "release-tip"
    assert db.get_session_by_title("Bot Chat") is None


def test_deferred_wake_keeps_its_home_after_scope_changes(owner_home, native_owner, monkeypatch, tmp_path):
    home, _db = owner_home
    native_owner.ready = False
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    path = home / "cron" / "bot_chat_pending" / (receipt_key(job) + ".json")
    native_owner.ready = True
    other = tmp_path / "different-root"
    monkeypatch.setenv("HERMES_HOME", str(other))
    pending._drain_one(path)
    assert native_owner.admissions[0]["session_id"] == "release-chat"
    assert get_hermes_home() == other
    assert not (other / "state.db").exists()


def test_same_execution_cannot_replace_a_deferred_payload(owner_home, native_owner):
    native_owner.ready = False
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "First", "release-chat")
    result = session_delivery.deliver_to_session(job, "Changed", "release-chat")
    assert "delivery id already belongs to a different payload" in result
    assert pending.read_pending(receipt_key(job))["content"] == "First"
    assert native_owner.admissions == []


def test_old_claimed_record_is_never_replayed(owner_home, native_owner):
    from utils import atomic_json_write
    native_owner.ready = False
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    key = receipt_key(job)
    record = pending.read_pending(key)
    record["status"] = "claimed"
    atomic_json_write(owner_home[0] / "cron" / "bot_chat_pending" / (key + ".json"), record)
    native_owner.ready = True
    pending.drain_legacy_pending()
    assert "claimed" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    assert native_owner.admissions == []


def test_pre_cutover_transferred_record_without_native_receipt_is_not_replayed(owner_home, native_owner):
    from utils import atomic_json_write
    native_owner.ready = False
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    key = receipt_key(job)
    record = pending.read_pending(key)
    record.update(status="transferred", receipt_status="claimed")
    atomic_json_write(owner_home[0] / "cron" / "bot_chat_pending" / (key + ".json"), record)
    native_owner.ready = True
    pending.drain_legacy_pending()
    assert "claimed" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    assert native_owner.admissions == []


def test_empty_session_target_in_legacy_record_never_becomes_bot_chat(owner_home, native_owner):
    record = {"id": "a" * 64, "status": "queued", "home": str(owner_home[0]),
              "content": "Wake", "session_id": "", "job": {"id": "toy"}}
    assert pending._dispose(owner_home[0] / "unused.json", record) is True
    assert record["status"] == "ambiguous"
    assert native_owner.admissions == []


def test_deleted_session_fails_preflight_without_a_replacement(owner_home):
    from cron.scheduler_preflight import _preflight_check_delivery
    assert "same profile" in _preflight_check_delivery({"deliver": "session:missing"})
    assert owner_home[1].get_session("missing") is None
