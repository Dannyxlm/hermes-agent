"""Close direct script-wake bookkeeping using the exact durable session receipt.

Canonical agent deliveries remain owned by delivery_queue/delivery_outcome. This bridge is
only for no-agent direct delivery, which deliberately has no canonical completion stamp.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def settle_pending(path: Path, record: dict) -> bool:
    """Under the pending lock, settle only its current producer receipt; never advance a fire."""
    from cron.executions import get_execution, latest_execution, settle_delivery_outcome
    from cron.jobs import _with_job, save_jobs, use_cron_store
    from cron.session_delivery import _delivery_key
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    if (record.get("status") not in {"settled", "failed"}
            or not record.get("admission_id") or not record.get("session_id")):
        return False
    home = Path(record["home"]).resolve()
    producer = record.get("job") or {}
    execution_id = producer.get("execution_id")
    expected = _delivery_key(home, producer, record["session_id"], bool(record.get("for_failure")))
    if (not execution_id or record.get("id") != expected
            or path.resolve() != (home / "cron" / "bot_chat_pending" / (expected + ".json")).resolve()):
        return False
    target = "session:" + record["session_id"]

    def apply(jobs, _i, job):
        queued = dict(job.get("last_delivery_queued") or {})
        receipt = queued.get(target) or {}
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
        queued.pop(target)
        error = job.get("last_delivery_error")
        if record["status"] == "failed":
            detail = record.get("error") or "scheduled wake terminal outcome unverified"
            error = f"{error}; {target}: {detail}" if error else f"{target}: {detail}"
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

    for target, receipt in (job.get("last_delivery_queued") or {}).items():
        if target.startswith("session:") and receipt.get("delivery_id"):
            try:
                record = read_pending(receipt["delivery_id"])
                if record is not None and record.get("status") in {"settled", "failed"}:
                    drain_pending(receipt["delivery_id"])
            except Exception:
                logger.warning("Scheduled wake outcome write-back failed for job %s", job.get("id"), exc_info=True)
