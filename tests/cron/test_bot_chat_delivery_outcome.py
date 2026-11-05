"""Exact downstream mailbox receipts close cron bookkeeping without another admission."""
import copy

import pytest

from cron import bot_chat_legacy as pending, executions, jobs, scheduler, session_delivery
from tests.cron import test_session_delivery_outcome as outcome_fixtures
from tools import bot_live_delivery as mailbox
from utils import atomic_json_write

owner_home = outcome_fixtures.owner_home


@pytest.fixture
def bot_run(owner_home, monkeypatch):
    from gateway import config

    home, _db = owner_home
    monkeypatch.setattr(scheduler, "load_config", dict)
    monkeypatch.setattr(config, "load_gateway_config", lambda: None)
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kw: (True, "raw", "Gate ready", None))
    (home / "scripts").mkdir()
    (home / "scripts" / "wake.py").write_text("print('Gate ready')\n")
    admitted = []
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: {"session_id": "bot"})

    def deliver(target_home, owner, message, *, delivery_id, notification_category="result"):
        admitted.append(delivery_id)
        record = dict(delivery_id=delivery_id, status="queued", session_id="bot", message=message,
                      admission_id="admission-" + delivery_id, notification_category=notification_category)
        with mailbox._locked(target_home) as root:
            mailbox._write(root / (delivery_id + ".json"), record)
        return record

    monkeypatch.setattr(mailbox, "deliver_to_live_owner", deliver)

    def run(target="bot-chat"):
        job = jobs.create_job("", "5m", script="wake.py", no_agent=True, repeat=1, deliver=target)
        scheduler.run_one_job(job)
        return job["id"], admitted[-1]

    return run, admitted


def terminal(home, key, *, status="settled"):
    record = mailbox.read_delivery_result(home, key)
    record.update(status=status, error="interrupted" if status == "failed" else "cancelled" if status == "cancelled" else None)
    with mailbox._locked(home) as root:
        mailbox._write(root / (key + ".json"), record)


@pytest.mark.parametrize("status", ["settled", "failed", "cancelled"])
@pytest.mark.parametrize("kind", ["direct", "transferred", "legacy_transferred", "cross_profile", "cross_profile_direct"])
def test_bot_chat_terminal_receipt_closes_producer(owner_home, bot_run, monkeypatch, tmp_path, status, kind):
    home, _db = owner_home
    target_home = home
    target = "bot-chat"
    if kind.startswith("cross_profile"):
        target_home = tmp_path / "target"
        target_home.mkdir()
        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: target_home)
        monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
        target = "bot-chat:target"
    run, admitted = bot_run
    job_id, key = run(target)
    initial = jobs.get_job(job_id)
    row = executions.latest_execution(job_id)
    if kind in {"transferred", "legacy_transferred", "cross_profile"}:
        producer = dict(id=job_id, name=initial["name"], execution_id=row["id"])
        record = pending.defer(key, producer, "Gate ready", target_home, for_failure=False)
        record.update(status="transferred", receipt_status="queued")
        if kind != "legacy_transferred":
            record["admission_id"] = "admission-" + key
        atomic_json_write(home / "cron" / "bot_chat_pending" / (key + ".json"), record)
    terminal(target_home, key, status=status)
    pending.drain_legacy_pending()
    saved = jobs.get_job(job_id)
    assert saved["last_status"] == ("ok" if status == "settled" else "delivery_failed")
    assert saved["last_delivery_queued"] is None
    assert saved["last_delivery_error"] is None if status == "settled" else ("cancelled" if status == "cancelled" else "interrupted") in saved["last_delivery_error"]
    assert executions.latest_execution(job_id)["delivery_outcome"] == ("delivered" if status == "settled" else "failed")
    assert saved["repeat"] == initial["repeat"] == {"times": 1, "completed": 1}
    assert saved["last_run_at"] == initial["last_run_at"]
    assert saved["state"] == "completed" and not saved["enabled"]
    if kind in {"transferred", "legacy_transferred", "cross_profile"}:
        record = pending.read_pending(key)
        assert record["status"] == ("settled" if status == "settled" else "failed") and record["bookkeeping_settled"]
        assert record["receipt_status"] == status
    pending.drain_legacy_pending()
    assert jobs.get_job(job_id) == saved
    assert admitted == [key]
    if target_home != home:
        assert not (target_home / "cron" / "jobs.json").exists()


@pytest.mark.parametrize("foreign", ["message", "id", "delivery_id", "admission", "missing", "claimed", "ambiguous"])
def test_transferred_receipt_is_observed_not_replayed(owner_home, bot_run, foreign):
    home, _db = owner_home
    run, admitted = bot_run
    job_id, key = run()
    initial = jobs.get_job(job_id)
    row = executions.latest_execution(job_id)
    record = pending.defer(key, dict(id=job_id, name=initial["name"], execution_id=row["id"]),
                           "Gate ready", home, for_failure=False)
    record["status"] = "transferred"
    if foreign == "admission":
        record["admission_id"] = "another-admission"
    path = home / "cron" / "bot_chat_pending" / (key + ".json")
    atomic_json_write(path, record)
    with mailbox._locked(home) as root:
        receipt_path = root / (key + ".json")
        receipt = mailbox._read(receipt_path)
        if foreign == "missing":
            receipt_path.unlink()
        else:
            receipt.update(status=foreign if foreign in {"claimed", "ambiguous"} else "settled")
            if foreign == "message":
                receipt["message"] = "another payload"
            if foreign == "id":
                receipt["id"] = "f" * 64
            if foreign == "delivery_id":
                receipt["delivery_id"] = "f" * 64
            mailbox._write(receipt_path, receipt)
    pending.drain_legacy_pending()
    assert pending.read_pending(key) == record
    # Direct producer observation also refuses a foreign identity/nonterminal receipt.
    assert jobs.get_job(job_id) == initial
    assert executions.latest_execution(job_id)["delivery_outcome"] == "queued"
    assert admitted == [key]


def test_legacy_session_mailbox_closes_without_native_admission(owner_home, bot_run):
    home, _db = owner_home
    run, admitted = bot_run
    job_id, _bot_key = run()
    saved = jobs.get_job(job_id)
    row = executions.latest_execution(job_id)
    producer = dict(id=job_id, name=saved["name"], execution_id=row["id"])
    key = session_delivery._delivery_key(home, producer, "release-chat", False)
    record = pending.defer(key, producer, "Gate ready", home, session_id="release-chat", for_failure=False)
    record["status"] = "transferred"
    atomic_json_write(home / "cron" / "bot_chat_pending" / (key + ".json"), record)
    jobs.update_job(job_id, {"last_delivery_queued": {"session:release-chat": {"status": "queued", "delivery_id": key}}})
    with mailbox._locked(home) as root:
        mailbox._write(root / (key + ".json"), dict(id=key, status="settled", session_id="release-chat",
            message=session_delivery.wake_message(producer, "Gate ready")))
    pending.drain_legacy_pending()
    assert jobs.get_job(job_id)["last_status"] == "ok"
    assert executions.latest_execution(job_id)["delivery_outcome"] == "delivered"
    assert pending.read_pending(key)["bookkeeping_settled"]
    assert len(admitted) == 1


def test_old_bot_mailbox_cannot_close_new_execution(owner_home, bot_run):
    home, _db = owner_home
    run, admitted = bot_run
    job_id, key = run()
    old = copy.deepcopy(jobs.get_job(job_id)["last_delivery_queued"])
    scheduler.run_one_job(jobs.get_job(job_id))
    saved = jobs.get_job(job_id)
    row = executions.latest_execution(job_id)
    # Even a stale upstream snapshot must not attach the old receipt to the new ledger row.
    jobs.update_job(job_id, {"last_delivery_queued": old})
    initial = jobs.get_job(job_id)
    terminal(home, key)
    pending.drain_legacy_pending()
    assert jobs.get_job(job_id) == initial
    assert executions.latest_execution(job_id) == row
    assert len(admitted) == 2
    assert saved["last_delivery_queued"] != old


def test_terminal_before_producer_bookkeeping_closes_after_run(owner_home, bot_run, monkeypatch):
    home, _db = owner_home
    run, admitted = bot_run
    real_mark = scheduler.mark_job_run

    def mark(*args, **kwargs):
        terminal(home, admitted[-1])
        pending.drain_legacy_pending()  # The running execution must not be settled yet.
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(scheduler, "mark_job_run", mark)
    job_id, key = run()
    assert jobs.get_job(job_id)["last_status"] == "ok"
    assert jobs.get_job(job_id)["last_delivery_queued"] is None
    assert executions.latest_execution(job_id)["delivery_outcome"] == "delivered"
    assert admitted == [key]


def test_terminal_pending_retries_writeback_without_replay(owner_home, bot_run, monkeypatch):
    home, _db = owner_home
    run, admitted = bot_run
    job_id, key = run()
    saved = jobs.get_job(job_id)
    producer = dict(id=job_id, name=saved["name"], execution_id=executions.latest_execution(job_id)["id"])
    record = pending.defer(key, producer, "Gate ready", home, for_failure=False)
    record["status"] = "transferred"
    atomic_json_write(home / "cron" / "bot_chat_pending" / (key + ".json"), record)
    terminal(home, key)
    with monkeypatch.context() as patch:
        def failed_save(*args):
            raise OSError("injected store failure")
        patch.setattr(jobs, "save_jobs", failed_save)
        with pytest.raises(OSError, match="injected store failure"):
            pending.drain_pending(key)
    assert pending.read_pending(key)["status"] == "settled"
    assert jobs.get_job(job_id)["last_status"] == "delivery_queued"
    pending.drain_legacy_pending()
    assert jobs.get_job(job_id)["last_status"] == "ok"
    assert pending.read_pending(key)["bookkeeping_settled"]
    assert admitted == [key]
