"""Drain work converges without replay, stale reads, or losing producer retries."""
import os
from pathlib import Path

import pytest

from cron import bot_chat_legacy as pending, executions, jobs, session_delivery
from utils import atomic_json_write


@pytest.fixture
def drain_home(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with jobs.use_cron_store(home):
        yield home


def terminal_record(home, index=0, *, status="settled", admission=True, execution_id=None):
    producer = {"id": "producer-" + str(index), "execution_id": execution_id or "run-" + str(index)}
    key = session_delivery._delivery_key(home, producer, "release-chat", False)
    record = dict(id=key, job=producer, home=str(home), content="keep this evidence",
                  status=status, session_id="release-chat", for_failure=False, sequence=index,
                  error="interrupted" if status == "failed" else None)
    if admission:
        record.update(admission_id="admission-" + key, admission_outcome="completed" if status == "settled" else "interrupted")
    path = home / "cron" / "bot_chat_pending" / (key + ".json")
    atomic_json_write(path, record)
    return path, record


def save_store(home, records):
    atomic_json_write(home / "cron" / "jobs.json", {"jobs": records})


def producer_job(record, **overrides):
    job = dict(id=record["job"]["id"], no_agent=True, last_status="delivery_queued",
               last_delivery_error=None, last_delivery_queued={
                   "session:release-chat": {"delivery_id": record["id"], "status": "queued"}})
    return job | overrides


@pytest.mark.parametrize("status", ["settled", "failed"])
@pytest.mark.parametrize("admission", [True, False])
def test_deleted_terminal_producer_closes_before_observation(drain_home, monkeypatch, status, admission):
    from cron import bot_chat_outcome

    path, record = terminal_record(drain_home, status=status, admission=admission)
    save_store(drain_home, [])
    observations = []
    monkeypatch.setattr(bot_chat_outcome, "observe_pending", lambda r: observations.append(r["id"]) or False)
    monkeypatch.setattr(session_delivery, "transfer_pending", lambda p, r: observations.append(r["id"]) or False)
    pending.drain_legacy_pending()
    saved = pending.read_pending(record["id"])
    assert saved.get("bookkeeping_closed") == "job_missing"
    assert not saved.get("bookkeeping_settled")
    assert {k: saved[k] for k in record} == record
    assert observations == []
    writes = []
    drained = []
    original = pending.atomic_json_write
    original_drain = pending._drain_one

    def drain(target):
        drained.append(target)
        return original_drain(target)

    monkeypatch.setattr(pending, "_drain_one", drain)
    monkeypatch.setattr(pending, "atomic_json_write", lambda *a, **kw: writes.append(a[0]) or original(*a, **kw))
    pending.drain_legacy_pending()
    assert drained == []
    pending.drain_pending(record["id"])
    assert pending.read_pending(record["id"]) == saved
    assert writes == []
    assert path.exists()


@pytest.mark.parametrize("kind,reason", [("agent", "not_direct"), ("replaced_target", "target_not_current"), ("superseded", "superseded")])
def test_inapplicable_terminal_bookkeeping_keeps_delivery_evidence(drain_home, kind, reason):
    path, record = terminal_record(drain_home, status="failed")
    job = producer_job(record)
    if kind == "agent":
        job["no_agent"] = False
    elif kind == "replaced_target":
        job["last_delivery_queued"]["session:release-chat"]["delivery_id"] = "f" * 64
    else:
        executions.create_execution(job["id"], source="direct", job=job)
    save_store(drain_home, [job])
    initial = jobs.get_job(job["id"])
    pending.drain_legacy_pending()
    saved = pending.read_pending(record["id"])
    assert saved.get("bookkeeping_closed") == reason
    assert not saved.get("bookkeeping_settled")
    assert {k: saved[k] for k in record} == record
    assert jobs.get_job(job["id"]) == initial
    assert path.exists()


def test_unchanged_jobs_are_parsed_once_per_drain(drain_home, monkeypatch):
    records = [terminal_record(drain_home, i)[1] for i in range(8)]
    save_store(drain_home, [producer_job(r, last_status=None) for r in records])
    parses = []
    original = jobs._parse_jobs_file

    def parse(path, **kwargs):
        parses.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(jobs, "_parse_jobs_file", parse)
    pending.drain_legacy_pending()
    assert parses == [drain_home / "cron" / "jobs.json"]
    for record in records:
        assert pending.read_pending(record["id"]) == record
    parses.clear()
    pending.drain_legacy_pending()
    assert parses == [drain_home / "cron" / "jobs.json"]


@pytest.mark.parametrize("rewrite", ["atomic", "in_place", "same_metadata"])
def test_changed_jobs_are_fresh_with_same_size_and_mtime(drain_home, monkeypatch, rewrite):
    first = terminal_record(drain_home, 0)[1]
    second = terminal_record(drain_home, 1)[1]
    initial = [producer_job(first, last_status=None), producer_job(second, no_agent=False)]
    save_store(drain_home, initial)
    original = pending._drain_one
    jobs_path = drain_home / "cron" / "jobs.json"
    seen = []

    def drain(path):
        if path.name == second["id"] + ".json":
            before = jobs_path.stat()
            text = jobs_path.read_text()
            # Keep the serialized size while changing the terminal producer's eligibility.
            text = text.replace('"no_agent": false', '"no_agent": true ')
            if rewrite == "atomic":
                replacement = jobs_path.with_suffix(".new")
                replacement.write_text(text)
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                replacement.replace(jobs_path)
            else:
                jobs_path.write_text(text)
                os.utime(jobs_path, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert jobs_path.stat().st_size == before.st_size
            assert jobs_path.stat().st_mtime_ns == before.st_mtime_ns
            if rewrite == "same_metadata":
                stat = Path.stat
                monkeypatch.setattr(Path, "stat", lambda path, **kw: before if path == jobs_path else stat(path, **kw))
        result = original(path)
        seen.append(result)
        return result

    monkeypatch.setattr(pending, "_drain_one", drain)
    pending.drain_legacy_pending()
    assert [r["id"] for r in seen] == [first["id"], second["id"]]
    assert seen[1].get("bookkeeping_settled") is True
    assert not seen[1].get("bookkeeping_closed")
    assert jobs.get_job(second["job"]["id"])["last_status"] == "ok"


def test_detached_job_values_do_not_poison_later_settlement(drain_home, monkeypatch):
    record = terminal_record(drain_home)[1]
    job = producer_job(record)
    save_store(drain_home, [job])
    original = pending._drain_one

    def drain(path):
        loaded = jobs.load_jobs()
        loaded[0]["no_agent"] = False
        loaded[0]["last_delivery_queued"].clear()
        return original(path)

    monkeypatch.setattr(pending, "_drain_one", drain)
    pending.drain_legacy_pending()
    assert pending.read_pending(record["id"])["bookkeeping_settled"]
    assert jobs.get_job(job["id"])["last_status"] == "ok"


def test_running_producer_without_current_target_retries_after_bookkeeping(drain_home):
    row = executions.create_execution("producer-0", source="direct", job=None)
    path, record = terminal_record(drain_home, execution_id=row["id"], status="failed")
    save_store(drain_home, [producer_job(record, last_delivery_queued=None, last_status="ok")])
    pending.drain_legacy_pending()
    assert pending.read_pending(record["id"]) == record
    executions.finish_execution(row["id"], success=True, delivery_outcome="queued")
    save_store(drain_home, [producer_job(record)])
    pending.drain_legacy_pending()
    saved = pending.read_pending(record["id"])
    assert saved.get("bookkeeping_settled") is True
    assert not saved.get("bookkeeping_closed")
    assert saved["status"] == "failed"
    assert jobs.get_job(record["job"]["id"])["last_status"] == "delivery_failed"
    assert executions.get_execution(row["id"])["delivery_outcome"] == "failed"
    assert path.exists()


@pytest.mark.parametrize("status", ["claimed", "ambiguous", "unknown", "settled", "failed"])
def test_never_replays_uncertain_or_terminal_records(drain_home, monkeypatch, status):
    from cron import bot_chat_outcome
    from tools import bot_live_delivery

    record = terminal_record(drain_home, status=status)[1]
    save_store(drain_home, [])
    calls = []
    monkeypatch.setattr(bot_live_delivery, "deliver_to_live_owner", lambda *a, **kw: calls.append("deliver"))
    monkeypatch.setattr(session_delivery, "transfer_pending", lambda *a: calls.append("transfer") or False)
    monkeypatch.setattr(bot_chat_outcome, "observe_pending", lambda *a: calls.append("observe") or False)
    pending.drain_legacy_pending()
    pending.drain_pending(record["id"])
    saved = pending.read_pending(record["id"])
    assert saved["status"] == status
    assert saved["content"] == record["content"]
    assert calls == []


def test_write_during_initial_read_is_not_certified_as_seen(drain_home, monkeypatch):
    record = terminal_record(drain_home)[1]
    save_store(drain_home, [producer_job(record, no_agent=False)])
    original = jobs._parse_jobs_file
    reads = []

    def parse(path, **kwargs):
        result = original(path, **kwargs)
        reads.append(path)
        if len(reads) == 1:
            save_store(drain_home, [producer_job(record)])
        return result

    monkeypatch.setattr(jobs, "_parse_jobs_file", parse)
    pending.drain_legacy_pending()
    assert pending.read_pending(record["id"])["bookkeeping_settled"]
    assert jobs.get_job(record["job"]["id"])["last_status"] == "ok"


def test_drain_jobs_reads_remain_profile_scoped(drain_home, tmp_path, monkeypatch):
    record = terminal_record(drain_home)[1]
    save_store(drain_home, [producer_job(record)])
    other = tmp_path / "other"
    save_store(other, [producer_job(record, no_agent=False)])
    original = pending._drain_one

    def drain(path):
        with jobs.use_cron_store(other):
            assert jobs.load_jobs()[0]["no_agent"] is False
        assert jobs.load_jobs()[0]["no_agent"] is True
        return original(path)

    monkeypatch.setattr(pending, "_drain_one", drain)
    pending.drain_legacy_pending()
    assert pending.read_pending(record["id"])["bookkeeping_settled"]
    with jobs.use_cron_store(other):
        assert jobs.load_jobs()[0]["last_status"] == "delivery_queued"


def test_new_terminal_observation_survives_jobs_read_failure(drain_home, monkeypatch):
    path, record = terminal_record(drain_home, status="failed")
    record["status"] = "transferred"
    atomic_json_write(path, record)
    save_store(drain_home, [producer_job(record)])

    def observe(_path, value):
        value["status"] = "failed"
        return True

    with monkeypatch.context() as patch:
        patch.setattr(session_delivery, "transfer_pending", observe)
        patch.setattr(jobs, "load_jobs", lambda: (_ for _ in ()).throw(RuntimeError("store unavailable")))
        with pytest.raises(RuntimeError, match="store unavailable"):
            pending.drain_pending(record["id"])
    saved = pending.read_pending(record["id"])
    assert saved["status"] == "failed"
    assert not saved.get("bookkeeping_closed")
    assert not saved.get("bookkeeping_settled")
    pending.drain_legacy_pending()
    assert pending.read_pending(record["id"])["bookkeeping_settled"]
    assert jobs.get_job(record["job"]["id"])["last_status"] == "delivery_failed"


def test_failed_terminal_is_not_relabelled_delivered_by_conflicting_mailbox(drain_home, monkeypatch):
    from cron import bot_chat_outcome

    record = terminal_record(drain_home, status="failed", admission=False)[1]
    job = producer_job(record)
    save_store(drain_home, [job])
    monkeypatch.setattr(bot_chat_outcome, "_terminal_mailbox", lambda home, key: dict(
        delivery_id=key, status="settled", session_id="release-chat",
        message=session_delivery.wake_message(record["job"], record["content"])))
    pending.drain_legacy_pending()
    assert pending.read_pending(record["id"]) == record
    assert jobs.get_job(job["id"])["last_status"] == "delivery_queued"
    assert jobs.get_job(job["id"])["last_delivery_queued"] == job["last_delivery_queued"]
