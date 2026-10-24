"""Exact-session scheduled wakes over the existing durable chat delivery lanes."""
from __future__ import annotations

import hashlib
import json
import logging
import re
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


def wake_message(job: dict, content: str) -> str:
    from cron.scheduler_delivery import _redact_cron_payload
    run_id = job.get("execution_id") or job.get("_session_run_id") or "?"
    return _redact_cron_payload(
        f'[Scheduled wake from cron job "{job.get("name", job.get("id", "?"))}" '
        f'({run_id}), not the user.]\n\n{content}', "session wake")


def _delivery_key(home: Path, job: dict, session_id: str, for_failure: bool) -> str:
    run_id = job.get("execution_id") or job.get("_session_run_id")
    return hashlib.sha256(json.dumps(
        [str(home), job.get("id"), str(run_id), session_id, for_failure],
        separators=(",", ":")).encode()).hexdigest()


def _wake_subject(home: Path, session_id: str) -> str | None:
    """Recover only the exact row's local login stamp, never a caller-supplied principal."""
    # Job creation is host-equivalent; the wake acts for the target row's owner.
    # Multi-login job isolation would require a creator principal stamped at creation.
    from gateway.session_identity import authenticated_subject, parse_authenticated_subject
    from hermes_state import SessionDB
    from hermes_state_local_migration import LEGACY_LOCAL_SOURCES, legacy_account_label

    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        row = db.get_session(session_id)
    finally:
        db.close()
    if not row or row['source'] not in LEGACY_LOCAL_SOURCES | {'local'}:
        return None
    owner = row.get('user_id')
    if parse_authenticated_subject(owner) is not None:
        return owner
    if (row['source'] in {'tui', 'gui', 'desktop'} and isinstance(owner, str)
            and not legacy_account_label(row)):
        provider, sep, user = owner.partition(':')
        if sep and provider and user and provider not in {'uid', 'auth', 'local'}:
            subject = authenticated_subject({'provider': provider, 'user_id': user})
            from hermes_state_mutation_binding import _login_label
            if _login_label(subject) == owner:
                return subject
    return None


def _wake_binding(path: Path, home: Path, record: dict) -> tuple[str, str | None]:
    from hermes_cli.gateway_client import GatewayRPCError

    expected = _delivery_key(home, record.get('job') or {}, record['session_id'], bool(record.get('for_failure')))
    if (record['id'] != expected
            or path.resolve() != (home / 'cron' / 'bot_chat_pending' / (expected + '.json')).resolve()):
        raise GatewayRPCError('permission_denied')
    target = resolve_session(record['session_id'], home)
    subject = _wake_subject(home, record['session_id'])
    if (_wake_subject(home, target) != subject
            or ('wake_subject' in record and record['wake_subject'] != subject)
            or (not record.get('admission_id') and record.get('target_session_id') not in (None, target))):
        raise GatewayRPCError('permission_denied')
    return target, subject


def _admission_status(receipt: dict) -> str:
    status = receipt["status"]
    if status == "terminal":
        return "settled" if receipt.get("outcome") == "completed" else "failed"
    return {"queued": "queued", "started": "claimed", "unknown": "ambiguous"}[status]


async def _wake_rpc(home: Path, record: dict, save) -> dict | None:
    """Use only a discovered owner; attaching and closing this viewer never starts inference itself."""
    from hermes_cli.gateway_client import connect_gateway
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import asyncio

    discovery = await asyncio.to_thread(discover_gateway_endpoint, home, timeout=5)
    if discovery.state != "ready" or discovery.endpoint is None:
        return None
    target_session = await asyncio.to_thread(resolve_session, record["session_id"], home)
    token = set_hermes_home_override(home)
    try:
        forwarded = {'subject': record['wake_subject']} if record.get('wake_subject') else {}
        async with connect_gateway(endpoint=discovery.endpoint, **forwarded) as client:
            snapshot = await client.rpc("session.resume",
                session_id=target_session,
                omit_messages=True, defer_history=True)
            target = snapshot["session_id"]
            if record.get("target_session_id") not in (None, target):
                raise ValueError("scheduled wake authority target changed")
            if record.get("admission_id"):
                return await client.rpc("prompt.receipt", session_id=target,
                                        admission_id=record["admission_id"])
            # Persist before the only inference-admitting RPC. A lost answer is never permission
            # to try another turn; old claimed records and this claim survive process crashes.
            record.update(status="claimed", target_session_id=target)
            await asyncio.to_thread(save)
            return await client.rpc("prompt.submit", session_id=target,
                submission_id="cron-session:" + record["id"], queued=True, finite=True,
                text=wake_message(record.get("job") or {}, record["content"]))
    finally:
        reset_hermes_home_override(token)


def transfer_pending(path: Path, record: dict) -> bool:
    """Admit or observe an exact-session wake under the pending-record lock, never a CLI writer."""
    from cron.scheduler_delivery import BOT_CHAT_POLICY_PLATFORM
    from gateway.warning_notifications import warning_notifications_enabled
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.gateway_client import GatewayRPCError
    from utils import atomic_json_write
    import asyncio

    home = Path(record["home"]).resolve()
    if not record.get("admission_id"):
        try:
            resolve_session(record["session_id"], home)
        except ValueError:
            record.update(status="ambiguous", error="scheduled wake target no longer exists in its profile")
            return True
    if not record.get("admission_id") and record.get("for_failure") and not warning_notifications_enabled(
            BOT_CHAT_POLICY_PLATFORM, load_user_config_effective(home / "config.yaml")):
        record.update(status="suppressed", error=None)
        return True
    def save():
        atomic_json_write(path, record, fsync_dir=True, mode=0o600)
    try:
        _target, subject = _wake_binding(path, home, record)
        if 'wake_subject' not in record:
            record['wake_subject'] = subject
            save()
        # The synchronous scheduler runs off-loop; never block its owning authority loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("scheduled wake drain must run off the owner event loop")
        receipt = asyncio.run(_wake_rpc(home, record, save))
        if receipt is None:
            return False
        if receipt["ref"]["session_id"] != record["target_session_id"]:
            raise ValueError("scheduled wake receipt belongs to a different session")
        status = _admission_status(receipt)
        record.update(admission_id=receipt["admission_id"], receipt_status=status,
                      status="transferred" if status in {"queued", "claimed"} else status, error=None)
        return True
    except Exception as exc:
        if isinstance(exc, GatewayRPCError) and str(exc) in {'not_found', 'permission_denied', 'profile_mismatch'}:
            if record.get('status') == 'claimed' or record.get('admission_id'):
                record.update(status='ambiguous', error='scheduled wake admission unverified; inspect before rearming')
            else:
                record.update(status='failed', error=f'scheduled wake ownership refused ({exc}); not delivered')
            save()
            return True
        if record.get("status") == "claimed":
            record.update(status="ambiguous", error="scheduled wake admission unverified; inspect before rearming")
            save()
        logger.warning("Scheduled wake %s not verified; retained %s", record.get("id"), record.get("status"), exc_info=True)
        return False


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
                       for_failure: bool = False) -> str | None:
    """Queue exact-session output through the canonical owner, retaining every uncertain receipt."""
    from cron.bot_chat_legacy import defer, drain_pending
    from cron.scheduler_delivery import BOT_CHAT_POLICY_PLATFORM, _redact_cron_payload
    from gateway.warning_notifications import warning_notifications_enabled
    from hermes_cli.config_effective import load_user_config_effective
    from tools.bot_live_delivery import read_delivery_result

    home = get_hermes_home().resolve()
    try:
        resolve_session(session_id, home)
        if not job.get("execution_id"):
            job.setdefault("_session_run_id", uuid.uuid4().hex)
        key = _delivery_key(home, job, session_id, for_failure)
        content = _redact_cron_payload(content, "session wake")
        message = wake_message(job, content)
        # A pre-cutover live mailbox claim remains authoritative; never start another turn.
        legacy = read_delivery_result(home, key)
        if legacy is not None:
            if legacy["message"] != message:
                raise ValueError("delivery id already belongs to a different payload")
            return _receipt(job, session_id, key, legacy["status"], legacy.get("error") or "")
        suppressed = for_failure and not warning_notifications_enabled(
            BOT_CHAT_POLICY_PLATFORM, load_user_config_effective(home / "config.yaml"))
        pending = defer(key, job, content, home, session_id=session_id,
                        for_failure=for_failure, suppressed=suppressed)
        if wake_message(pending.get("job") or {}, pending["content"]) != message:
            raise ValueError("delivery id already belongs to a different payload")
        pending = drain_pending(key) or pending
        status = (pending.get("receipt_status") or "ambiguous") if pending["status"] == "transferred" else pending["status"]
        return _receipt(job, session_id, key, status, pending.get("error") or "")
    except Exception as exc:
        logger.warning("Scheduled wake for session %s unverified", session_id, exc_info=True)
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
