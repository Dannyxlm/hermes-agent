"""Close direct script-delivery bookkeeping using exact durable chat receipts.

Canonical agent deliveries remain owned by delivery_queue/delivery_outcome. This bridge is
only for no-agent direct delivery, which deliberately has no canonical completion stamp.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def bookkeeping_closed_reason(path: Path, record: dict) -> str | None:
    """Classify inapplicable terminal work under the jobs lock, not as delivered."""
    from cron.executions import get_execution, latest_execution
    from cron.jobs import _with_job, use_cron_store
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    producer = record.get("job") or {}
    execution_id = producer.get("execution_id")

    def reason(_jobs, _i, job):
        if not job.get("no_agent"):
            return "not_direct"
        if not execution_id:
            return None
        latest = latest_execution(job["id"])
        if latest is not None and latest["id"] != execution_id:
            return "superseded"
        row = get_execution(execution_id)
        # No current target is normal before mark_job_run. Keep the producer's retry.
        if row is not None and row["status"] not in {"completed", "failed", "unknown"}:
            return None
        if not job.get("last_status"):
            return None
        queued = job.get("last_delivery_queued") or {}
        target = "session:" + record["session_id"] if record.get("session_id") else None
        receipts = [queued.get(target) or {}] if target else [
            receipt for key, receipt in queued.items() if key.startswith("bot-chat:")]
        if not any(receipt.get("delivery_id") == record.get("id") for receipt in receipts):
            return "target_not_current"
        return None

    home = path.resolve().parent.parent.parent
    token = set_hermes_home_override(home)
    try:
        with use_cron_store(home):
            return _with_job(producer.get("id"), reason, missing="job_missing")
    finally:
        reset_hermes_home_override(token)


def settle_pending(path: Path, record: dict) -> bool:
    """Under the pending lock, settle only its current producer receipt; never advance a fire."""
    from cron.session_delivery import _delivery_key

    if record.get("status") not in {"settled", "failed"}:
        return False
    home = path.resolve().parent.parent.parent
    producer = record.get("job") or {}
    if record.get("session_id"):
        if home != Path(record["home"]).resolve():
            return False
        expected = _delivery_key(home, producer, record["session_id"], bool(record.get("for_failure")))
        target = "session:" + record["session_id"]
    else:
        from cron.bot_chat_outcome import delivery_key
        expected = delivery_key(home, producer, Path(record["home"]).resolve())
        target = None
    if (record.get("id") != expected
            or path.resolve() != (home / "cron" / "bot_chat_pending" / (expected + ".json")).resolve()):
        return False
    return settle_receipt(home, producer, expected, target, record["status"], record.get("error"))


def settle_receipt(home: Path, producer: dict, expected: str, target: str | None,
                   status: str, detail: str | None) -> bool:
    """Shared exact-id/latest-execution fence for native and mailbox observations."""
    from cron.executions import get_execution, latest_execution, settle_delivery_outcome
    from cron.jobs import _with_job, save_jobs, use_cron_store
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    execution_id = producer.get("execution_id")
    if not execution_id or status not in {"settled", "failed"}:
        return False

    def apply(jobs, _i, job):
        queued = dict(job.get("last_delivery_queued") or {})
        receipt_target = target or next(
            (key for key, value in queued.items()
             if key.startswith("bot-chat:") and value.get("delivery_id") == expected), None)
        receipt = queued.get(receipt_target) or {}
        if not job.get("no_agent") or receipt.get("delivery_id") != expected:
            return False
        row = get_execution(execution_id)
        latest = latest_execution(job["id"])
        if latest is not None and latest["id"] != execution_id:
            return False
        # A drainer can beat mark_job_run/finish_execution. The producer calls us again
        # after both writes. Missing rows are allowed: retention can prune old attempts.
        if row is not None and (row["job_id"] != job["id"] or row["status"] not in {"completed", "failed", "unknown"}):
            return False
        if not job.get("last_status"):
            return False
        queued.pop(receipt_target)
        error = job.get("last_delivery_error")
        if status == "failed":
            error_detail = detail or "scheduled wake terminal outcome unverified"
            error = f"{error}; {receipt_target}: {error_detail}" if error else f"{receipt_target}: {error_detail}"
        outcome = "failed" if error else "queued" if queued else "delivered"
        # Ledger first: if the subsequent JSON save fails, the exact queued target still
        # fences a retry. Never invent a row or overwrite an already-terminal outcome.
        if outcome != "queued" and row is not None:
            settle_delivery_outcome(execution_id, outcome)
        job["last_delivery_queued"] = queued or None
        job["last_delivery_error"] = error
        if job["last_status"] in {"delivery_queued", "delivery_failed"}:
            job["last_status"] = "delivery_failed" if error else "delivery_queued" if queued else "ok"
        save_jobs(jobs)
        return True

    token = set_hermes_home_override(home)
    try:
        with use_cron_store(home):
            return bool(_with_job(producer.get("id"), apply, missing=False))
    finally:
        reset_hermes_home_override(token)


def settle_after_run(job: dict) -> None:
    """The other side of the drain/bookkeeping race; read receipts without admitting a turn."""
    if not job.get("no_agent"):
        return
    from cron.bot_chat_legacy import drain_pending, read_pending
    from cron.bot_chat_outcome import settle_bot_chat_job

    try:
        settle_bot_chat_job(job)
    except Exception:
        logger.warning("Bot Chat outcome write-back failed for job %s", job.get("id"), exc_info=True)
    for target, receipt in (job.get("last_delivery_queued") or {}).items():
        if target.startswith(("session:", "bot-chat:")) and receipt.get("delivery_id"):
            try:
                record = read_pending(receipt["delivery_id"])
                if record is not None and record.get("status") in {"transferred", "settled", "failed"}:
                    drain_pending(receipt["delivery_id"])
            except Exception:
                logger.warning("Scheduled wake outcome write-back failed for job %s", job.get("id"), exc_info=True)
