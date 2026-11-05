"""Observe exact mailbox receipts; bookkeeping recovery never admits another turn."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def delivery_key(source: Path, producer: dict, target: Path) -> str:
    """The direct Bot Chat producer identity (also used before live admission)."""
    run_id = producer.get("execution_id") or producer.get("_bot_chat_run_id")
    return hashlib.sha256(json.dumps(
        [str(source), producer.get("id", "?"), str(run_id), str(target)],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _terminal_mailbox(home: Path, key: str) -> dict | None:
    from tools.bot_live_delivery import read_delivery_result

    receipt = read_delivery_result(home, key)
    if (not receipt or receipt.get("delivery_id", receipt.get("id")) != key
            or receipt.get("id", key) != key
            or receipt.get("status") not in {"settled", "failed", "cancelled"}):
        return None
    return receipt


def observe_pending(record: dict) -> bool:
    """Observe a transferred receipt, never resubmit, even without an admission id."""
    from cron.scheduler_delivery import bot_chat_message
    from cron.session_delivery import wake_message

    receipt = _terminal_mailbox(Path(record["home"]), record["id"])
    message = (wake_message if record.get("session_id") else bot_chat_message)(
        record.get("job") or {}, record["content"])
    if (not receipt or receipt.get("message") != message
            or (record.get("admission_id") and receipt.get("admission_id") != record["admission_id"])
            or (record.get("session_id") and receipt.get("session_id") != record["session_id"])):
        return False
    record.update(status="settled" if receipt["status"] == "settled" else "failed",
                  receipt_status=receipt["status"],
                  error=receipt.get("error") or receipt.get("reason") or (
                      "Bot Chat delivery cancelled" if receipt["status"] == "cancelled" else None))
    return True


def settle_bot_chat_job(job: dict) -> None:
    """Live direct sends have no pending file; their current producer receipt is the fence."""
    from cron.bot_chat_legacy import _pending_path
    from cron.executions import latest_execution
    from cron.session_delivery_outcome import settle_receipt
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_hermes_home

    if not job.get("no_agent") or not job.get("last_delivery_queued"):
        return
    row = latest_execution(job["id"])
    if row is None:
        return  # Without either producer record or ledger, identity cannot be reconstructed.
    source = get_hermes_home().resolve()
    producer = dict(id=job["id"], execution_id=row["id"])
    for target, queued in job["last_delivery_queued"].items():
        key = queued.get("delivery_id")
        if not target.startswith("bot-chat:") or not key or _pending_path(key).exists():
            continue  # A parked payload is owned by its locked pending-record observer.
        profile = target.removeprefix("bot-chat:")
        home = source if profile == "(own)" else get_profile_dir(profile).resolve()
        if key != delivery_key(source, producer, home):
            continue
        receipt = _terminal_mailbox(home, key)
        if receipt:
            status = "settled" if receipt["status"] == "settled" else "failed"
            settle_receipt(source, producer, key, target, status,
                           receipt.get("error") or receipt.get("reason") or "Bot Chat delivery " + receipt["status"])


def settle_bot_chat_jobs() -> None:
    """Run with the tick's profile scope, including disabled/completed one-shot producers."""
    from cron.jobs import load_jobs

    for job in load_jobs():
        try:
            settle_bot_chat_job(job)
        except Exception:
            logger.warning("Bot Chat outcome write-back failed for job %s", job.get("id"), exc_info=True)
