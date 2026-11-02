"""Session admission completion closes producer bookkeeping without replaying the wake."""
import copy

import pytest

from cron import bot_chat_legacy as pending, executions, jobs, scheduler, session_delivery
from hermes_state import SessionDB
from tests.cron import test_session_delivery as session_fixtures
from utils import atomic_json_write

native_owner = session_fixtures.native_owner


@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with jobs.use_cron_store(home):
        db = SessionDB(db_path=home / "state.db")
        db.create_session(session_id="release-chat", source="desktop")
        yield home, db
        db.close()


@pytest.fixture
def script_run(owner_home, native_owner, monkeypatch):
    from gateway import config

    monkeypatch.setattr(scheduler, "load_config", dict)
    monkeypatch.setattr(config, "load_gateway_config", lambda: None)
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kw: (True, "raw", "Gate ready", None))
    home, _db = owner_home
    (home / "scripts").mkdir()
    (home / "scripts" / "wake.py").write_text("print('Gate ready')\n")

    def run():
        job = jobs.create_job("", "5m", script="wake.py", no_agent=True,
                              repeat=1, deliver="session:release-chat")
        scheduler.run_one_job(job)
        key = native_owner.admissions[-1]["submission_id"].removeprefix("cron-session:")
        return job["id"], key

    return run


@pytest.mark.parametrize("outcome", ["completed", "interrupted"])
@pytest.mark.parametrize("timing", ["after_run", "before_bookkeeping", "historical", "pruned_history"])
def test_session_terminal_closes_only_delivery_bookkeeping(
        owner_home, native_owner, script_run, monkeypatch, outcome, timing):
    if timing == "pruned_history":
        monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 0)
    if timing == "before_bookkeeping":
        real_mark = scheduler.mark_job_run

        def mark(*args, **kwargs):
            native_owner.status, native_owner.outcome = "terminal", outcome
            pending.drain_legacy_pending()
            return real_mark(*args, **kwargs)

        monkeypatch.setattr(scheduler, "mark_job_run", mark)
    job_id, key = script_run()
    initial = jobs.get_job(job_id)
    if timing != "before_bookkeeping":
        assert initial["last_status"] == "delivery_queued"
        native_owner.status, native_owner.outcome = "terminal", outcome
    if timing in {"historical", "pruned_history"}:
        # Old releases persisted terminal pending files but never closed the producer.
        path = owner_home[0] / "cron" / "bot_chat_pending" / (key + ".json")
        record = pending.read_pending(key)
        assert session_delivery.transfer_pending(path, record)
        record.pop("admission_outcome", None)
        record["error"] = None  # Old releases also discarded interrupted outcome detail.
        atomic_json_write(path, record)
    pending.drain_legacy_pending()
    saved = jobs.get_job(job_id)
    assert saved["last_status"] == ("ok" if outcome == "completed" else "delivery_failed")
    assert saved["last_delivery_queued"] is None
    assert saved["last_delivery_error"] is None if outcome == "completed" else "interrupted" in saved["last_delivery_error"]
    assert saved["repeat"] == {"times": 1, "completed": 1}
    assert saved["state"] == "completed" and saved["enabled"] is False
    assert saved["last_run_at"] == initial["last_run_at"]
    row = executions.latest_execution(job_id)
    if timing == "pruned_history":
        assert row is None  # Repair must not manufacture a pruned audit row.
    else:
        assert row["delivery_outcome"] == ("delivered" if outcome == "completed" else "failed")
    terminal_record = pending.read_pending(key)
    calls = list(native_owner.calls)
    pending.drain_legacy_pending()
    pending.drain_pending(key)
    assert jobs.get_job(job_id) == saved
    assert pending.read_pending(key) == terminal_record
    assert native_owner.calls == calls
    assert len(native_owner.admissions) == 1


def test_stale_receipt_cannot_close_a_later_run(owner_home, native_owner, script_run):
    job_id, old_key = script_run()
    old_record = pending.read_pending(old_key)
    old_receipt = copy.deepcopy(native_owner.receipt)
    job = jobs.get_job(job_id)
    scheduler.run_one_job(job)
    new_saved = jobs.get_job(job_id)
    new_row = executions.latest_execution(job_id)
    assert new_saved["last_delivery_queued"]["session:release-chat"]["delivery_id"] != old_key
    native_owner.receipt = old_receipt
    native_owner.status, native_owner.outcome = "terminal", "completed"
    pending.drain_pending(old_key)
    assert pending.read_pending(old_key)["status"] == "settled"
    assert jobs.get_job(job_id) == new_saved
    assert executions.get_execution(new_row["id"]) == new_row
    assert len(native_owner.admissions) == 2
    assert old_record["job"]["execution_id"] != new_row["id"]


@pytest.mark.parametrize("first_outcome", ["completed", "interrupted"])
def test_other_pending_targets_and_errors_survive(owner_home, native_owner, script_run, first_outcome):
    job_id, key = script_run()
    saved = jobs.get_job(job_id)
    queue = saved["last_delivery_queued"]
    queue["bot-chat:other"] = {"status": "queued", "delivery_id": "b" * 64}
    jobs.update_job(job_id, {"last_delivery_queued": queue, "last_delivery_error": "other transport refused"})
    native_owner.status, native_owner.outcome = "terminal", first_outcome
    pending.drain_pending(key)
    saved = jobs.get_job(job_id)
    assert saved["last_delivery_queued"] == {"bot-chat:other": queue["bot-chat:other"]}
    assert "other transport refused" in saved["last_delivery_error"]
    assert saved["last_status"] == "delivery_failed"
    assert executions.latest_execution(job_id)["delivery_outcome"] == "failed"


def test_other_pending_target_keeps_success_queued(owner_home, native_owner, script_run):
    job_id, key = script_run()
    queue = jobs.get_job(job_id)["last_delivery_queued"]
    queue["bot-chat:other"] = {"status": "queued", "delivery_id": "b" * 64}
    jobs.update_job(job_id, {"last_delivery_queued": queue})
    native_owner.status, native_owner.outcome = "terminal", "completed"
    pending.drain_pending(key)
    saved = jobs.get_job(job_id)
    assert saved["last_delivery_queued"] == {"bot-chat:other": queue["bot-chat:other"]}
    assert saved["last_status"] == "delivery_queued"
    assert executions.latest_execution(job_id)["delivery_outcome"] == "queued"


def test_generic_canonical_completion_fence_is_not_bypassed(owner_home, native_owner, script_run):
    from cron.delivery_outcome import _settle_job_record

    job_id, _key = script_run()
    row = executions.latest_execution(job_id)
    assert _settle_job_record(job_id, row["id"], None) is False
    assert jobs.get_job(job_id)["last_status"] == "delivery_queued"


@pytest.mark.parametrize("first_outcome", ["completed", "interrupted"])
def test_two_session_receipts_aggregate_without_losing_failure(owner_home, native_owner, script_run, first_outcome):
    owner_home[1].create_session(session_id="second-chat", source="desktop")
    job = jobs.create_job("", "5m", script="wake.py", no_agent=True, repeat=1,
                          deliver="session:release-chat,session:second-chat")
    scheduler.run_one_job(job)
    queued = jobs.get_job(job["id"])["last_delivery_queued"]
    assert set(queued) == {"session:release-chat", "session:second-chat"}
    for index, (target, receipt) in enumerate(queued.items()):
        key = receipt["delivery_id"]
        native_owner.receipt = {"admission_id": "wake-" + key, "ref": {"session_id": target.removeprefix("session:")}}
        native_owner.status = "terminal"
        native_owner.outcome = first_outcome if index == 0 else "completed"
        pending.drain_pending(key)
        saved = jobs.get_job(job["id"])
        expected = "delivery_failed" if first_outcome == "interrupted" else "delivery_queued" if index == 0 else "ok"
        assert saved["last_status"] == expected
    assert saved["last_delivery_queued"] is None
    if first_outcome == "interrupted":
        assert "interrupted" in saved["last_delivery_error"]
    assert executions.latest_execution(job["id"])["delivery_outcome"] == ("failed" if first_outcome == "interrupted" else "delivered")
    assert saved["repeat"] == {"times": 1, "completed": 1}


def test_wrong_admission_receipt_does_not_settle(owner_home, native_owner, script_run, monkeypatch):
    job_id, key = script_run()
    initial = jobs.get_job(job_id)

    async def wrong_receipt(*args):
        return {"admission_id": "foreign-admission", "status": "terminal", "outcome": "completed",
                "ref": {"session_id": "release-chat"}}

    monkeypatch.setattr(session_delivery, "_wake_rpc", wrong_receipt)
    pending.drain_pending(key)
    assert pending.read_pending(key)["status"] == "transferred"
    assert jobs.get_job(job_id) == initial
    assert executions.latest_execution(job_id)["delivery_outcome"] == "queued"
    assert len(native_owner.admissions) == 1


def test_writeback_retries_after_store_failure_without_replay(owner_home, native_owner, script_run, monkeypatch):
    job_id, key = script_run()
    native_owner.status, native_owner.outcome = "terminal", "completed"
    real_save = jobs.save_jobs
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "save_jobs", lambda *args: (_ for _ in ()).throw(OSError("injected store failure")))
        with pytest.raises(OSError, match="injected store failure"):
            pending.drain_pending(key)
    assert pending.read_pending(key)["status"] == "settled"
    assert jobs.get_job(job_id)["last_status"] == "delivery_queued"
    assert jobs.save_jobs is real_save
    pending.drain_legacy_pending()
    assert jobs.get_job(job_id)["last_status"] == "ok"
    assert len(native_owner.admissions) == 1


def test_terminal_writeback_uses_pending_home_not_callers(owner_home, native_owner, script_run, monkeypatch, tmp_path):
    job_id, key = script_run()
    native_owner.status, native_owner.outcome = "terminal", "completed"
    other = tmp_path / "other"
    monkeypatch.setenv("HERMES_HOME", str(other))
    path = owner_home[0] / "cron" / "bot_chat_pending" / (key + ".json")
    pending._drain_one(path)
    assert not (other / "cron").exists()
    monkeypatch.setenv("HERMES_HOME", str(owner_home[0]))
    assert jobs.get_job(job_id)["last_status"] == "ok"
