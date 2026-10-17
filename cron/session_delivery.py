"""Exact-session scheduled wakes over the existing durable chat delivery lanes."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def session_target(token: str) -> str | None:
    """Recognize the reserved session token without normalizing the identifier."""
    return token[len("session:"):] if token.startswith("session:") else None


def resolve_session(session_id: str, home: Path | None = None) -> str:
    """An exact id must exist in this profile's store, never title/prefix/global lookup."""
    from hermes_state import SessionDB

    home = home if home is not None else get_hermes_home().resolve()
    error = (
        f"Invalid session:<id> delivery target {session_id!r}: session must exist in the same profile. "
        "Use an exact ID from `hermes sessions list` in this profile, or select the owning profile first.")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None or not (home / "state.db").is_file():
        raise ValueError(error)
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        if db.get_session(session_id) is None:
            raise ValueError(error)
        tip = db.get_compression_tip(session_id)
        if tip is None or db.get_session(tip) is None:
            raise ValueError(error)
        return tip
    finally:
        db.close()


def validate_delivery(value):
    """Leave existing delivery grammars unchanged; validate session targets before storage."""
    parts = value if isinstance(value, (list, tuple)) else [value]
    for part in parts:
        for token in str(part or "").split(","):
            if (session_id := session_target(token.strip())) is not None:
                resolve_session(session_id)
    return value


def _resume_session(home: Path, session_id: str, message: str) -> str | None:
    from agent.delegation_context import delegated_child_subprocess_env
    from cron.scheduler_delivery import _format_failure_streams, _get_bot_chat_delivery_timeout, _run_bot_chat_turn
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
    from hermes_cli.quiet_single_query import TURN_REPORT_FILE_ENV
    from tools.environments.local import served_profile_child_env
    import os

    env = served_profile_child_env(
        delegated_child_subprocess_env(os.environ), target_home=home, inherit_credentials=True)
    pin_hermes_tree_on_pythonpath(env, Path(__file__).resolve().parents[1])
    # Exact resume reloads saved history and enforces the normal active-session writer lease.
    with tempfile.TemporaryDirectory(prefix="hermes-cron-session-") as scratch:
        query = Path(scratch) / "wake.txt"
        query.write_text(message, encoding="utf-8")
        report = str(Path(scratch) / "turn.json")
        env[TURN_REPORT_FILE_ENV] = report
        argv = [sys.executable, "-m", "hermes_cli.main"]
        # Named profile homes already pin startup; custom roots need to bypass sticky selection.
        if home.parent.name != "profiles":
            argv += ["-p", "default"]
        argv += ["chat", "--in", "~", "--resume", session_id, "-Q", "--query-file", str(query)]
        try:
            result = _run_bot_chat_turn(argv, env, report, _get_bot_chat_delivery_timeout())
        except subprocess.TimeoutExpired:
            return "scheduled wake timed out; outcome ambiguous, inspect the session before rearming"
        if result.returncode:
            return (f"scheduled wake failed ({_format_failure_streams(result)}); "
                    "inspect `hermes cron runs` and the session before rearming")
    return None


def _receipt(job: dict, session_id: str, key: str, status: str, error: str = "") -> str | None:
    target = f"session:{session_id}"
    job.setdefault("_bot_chat_delivery_receipts", {})[target] = {"status": status, "delivery_id": key}
    logger.info("Scheduled wake: job=%s target=%s receipt=%s status=%s", job.get("id"), target, key, status)
    if status == "suppressed":
        job["_notification_all_targets_suppressed"] = True
    if status in {"settled", "suppressed"}:
        return None
    return f"{target} {status} (receipt {key}): {error or 'completion unverified; do not resend'}"


def deliver_to_session(job: dict, content: str, session_id: str, *,
                       deferred: dict | None = None, for_failure: bool = False) -> str | None:
    """Queue while busy; only a durable deferred claim can start an unowned CLI turn."""
    from cron.bot_chat_delivery import defer, read_pending
    from cron.scheduler_delivery import BOT_CHAT_POLICY_PLATFORM, _redact_cron_payload
    from gateway.warning_notifications import warning_notifications_enabled
    from hermes_cli.config_effective import load_user_config_effective
    from tools import bot_live_delivery as mailbox

    home = Path(deferred["home"]).resolve() if deferred is not None else get_hermes_home().resolve()
    try:
        tip = resolve_session(session_id, home)
        run_id = job.get("execution_id") or job.setdefault("_session_run_id", uuid.uuid4().hex)
        key = (deferred["id"] if deferred else hashlib.sha256(json.dumps(
            [str(home), job.get("id"), str(run_id), session_id, for_failure],
            separators=(",", ":")).encode()).hexdigest())
        content = _redact_cron_payload(content, "session wake")
        message = _redact_cron_payload(
            f'[Scheduled wake from cron job "{job.get("name", job.get("id", "?"))}" '
            f'({run_id}), not the user.]\n\n{content}', "session wake")
        receipt = mailbox.read_delivery_result(home, key)
        if receipt is not None:
            if receipt["message"] != message:
                raise ValueError("delivery id already belongs to a different payload")
            return _receipt(job, session_id, key, receipt["status"], receipt.get("error", ""))
        if deferred is None:
            pending = read_pending(key)
            suppressed = for_failure and not warning_notifications_enabled(
                BOT_CHAT_POLICY_PLATFORM, load_user_config_effective(home / "config.yaml"))
            if pending is not None or suppressed:
                pending = defer(key, dict(job), content, "", home, session_id=session_id,
                                for_failure=for_failure, suppressed=suppressed)
            if pending is not None:
                return _receipt(job, session_id, key, pending["status"], pending.get("error") or "")
        owner = mailbox.find_session_live_owner(home, tip)
        if owner is not None:
            receipt = mailbox.deliver_to_live_owner(home, owner, message, delivery_id=key,
                **({"notification_category": "diagnostic"} if for_failure else {}))
            return _receipt(job, session_id, key, receipt["status"])
        if deferred is None:
            pending = defer(key, dict(job), content, "", home, session_id=session_id, for_failure=for_failure)
            return _receipt(job, session_id, key, pending["status"])
        logger.info("Scheduled wake: job=%s target=session:%s receipt=%s status=claimed", job.get("id"), tip, key)
        return _resume_session(home, tip, message)
    except Exception as exc:
        logger.exception("Scheduled wake for session %s failed", session_id)
        return f"session:{session_id} scheduled wake unverified: {exc}; inspect before rearming"


def deliver_chat_turn(job, content, target, *, for_failure, delivery_errors, unverified_targets) -> int:
    """Share admission outcome accounting for canonical and exact-session chat turns."""
    from cron.scheduler_delivery import _deliver_to_bot_chat

    deliver = deliver_to_session if target["platform"] == "session" else _deliver_to_bot_chat
    error = deliver(job, content, target["chat_id"], for_failure=for_failure)
    suppressed = job.pop("_notification_all_targets_suppressed", False)
    if error:
        receipt_target = f"{target['platform']}:{target['chat_id'] or '(own)'}"
        receipt = job.get("_bot_chat_delivery_receipts", {}).get(receipt_target)
        if not receipt or receipt["status"] not in ("queued", "claimed"):
            delivery_errors.append(error)
        if receipt and receipt["status"] == "ambiguous":
            unverified_targets.append(error)
    return int(suppressed)
