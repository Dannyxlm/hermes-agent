"""``cron/bot_chat_pending``: Bot Chat outputs whose target owner was not running yet.

``defer`` parks one (a cross-profile target whose gateway is down or still starting), and
the retired CLI lane left records of the same shape behind. Each still-``queued`` record is handed,
under its own id, to the target profile's live owner through the canonical
``tools.bot_live_delivery.deliver_to_live_owner`` door (idempotent per id). Records are
never deleted: they keep their final status as evidence, exactly as the old drain did.
A ``claimed`` record is an uncertain turn and is never resent. Exact-session wakes
retain their session id and are admitted/observed through the native authority, never Bot Chat.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from hermes_dir_acl import make_user_dir

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)
_warned: set[Path] = set()


def _pending_path(key: str) -> Path:
    import re
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32,64}", key) is None:
        raise ValueError("invalid pending delivery id")
    return get_hermes_home().resolve() / "cron" / "bot_chat_pending" / f"{key}.json"


def read_pending(key: str) -> dict | None:
    try:
        record = json.loads(_pending_path(key).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    if not isinstance(record, dict):
        raise ValueError("pending delivery is not a JSON object")
    return record


def defer(key: str, job: dict, content: str, home: Path, *, for_failure: bool,
          session_id: str | None = None, suppressed: bool = False) -> dict:
    """Park never-admitted output; retries retain the payload, target and final disposition."""
    import time
    from hermes_cli.active_sessions import _FileLock

    path = _pending_path(key)
    make_user_dir(path.parent, parents=True, exist_ok=True, mode=0o700)
    with _FileLock(path.with_suffix(".lock")):
        record = read_pending(key)
        if record is not None:
            if (record.get("content") != content or record.get("home") != str(home)
                    or bool(record.get("for_failure")) != for_failure
                    or record.get("session_id") != session_id):
                raise ValueError("delivery id already belongs to a different payload")
            if suppressed and record.get("status") == "queued":
                record.update(status="suppressed", error=None)
                atomic_json_write(path, record, fsync_dir=True, mode=0o600)
            return record
        record = dict(
            id=key, status="suppressed" if suppressed else "queued",
            job={k: job[k] for k in ("id", "name", "execution_id", "_session_run_id") if k in job},
            content=content, home=str(home), for_failure=for_failure, sequence=time.time_ns())
        if session_id is not None:
            record["session_id"] = session_id
        atomic_json_write(path, record, fsync_dir=True, mode=0o600)
        return record


def _queued(root: Path) -> list[tuple[Path, dict]]:
    found = []
    for path in root.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(record, dict):
                raise ValueError(f"expected a JSON object, got {type(record).__name__}")
        except (OSError, ValueError) as exc:
            if path not in _warned:
                _warned.add(path)
                logger.error("Unreadable legacy Bot Chat pending record %s kept as evidence: %s", path, exc)
            continue
        if record.get("status") == "queued" or (
                record.get("session_id") and record.get("admission_id") and record.get("status") == "transferred"):
            found.append((path, record))
    return sorted(found, key=lambda item: (item[1].get("sequence", 0), item[0].name))


def _malformed(record: dict) -> str:
    """Why *record* can never be handed off (empty when its required fields are usable)."""
    missing = [key for key in ("id", "home", "content")
               if not isinstance(record.get(key), str) or not record.get(key)]
    if missing:
        return f"malformed legacy bot-chat record (missing {', '.join(missing)}); not delivered"
    if not isinstance(record.get("job") or {}, dict):
        return "malformed legacy bot-chat record (job is not an object); not delivered"
    if "session_id" in record and (not isinstance(record["session_id"], str) or not record["session_id"]):
        return "malformed scheduled wake (session_id is empty or not a string); not delivered"
    return ""


def _dispose(path: Path, record: dict) -> bool:
    """Decide one queued record in place; False leaves it queued for a later tick."""
    from cron.scheduler_delivery import BOT_CHAT_POLICY_PLATFORM, bot_chat_message
    from gateway.warning_notifications import warning_notifications_enabled
    from hermes_cli.config_effective import load_user_config_effective
    from tools.bot_live_delivery import deliver_to_live_owner, find_canonical_live_owner

    if reason := _malformed(record):
        record.update(status="ambiguous", error=reason)
        return True
    if "session_id" in record:
        from cron.session_delivery import transfer_pending
        return transfer_pending(path, record)
    home = Path(record["home"]).resolve()
    for_failure = bool(record.get("for_failure"))
    if not (home / "state.db").is_file():
        record.update(status="ambiguous", error=f"bot-chat delivery target no longer exists: {home}")
    elif for_failure and not warning_notifications_enabled(
            BOT_CHAT_POLICY_PLATFORM, load_user_config_effective(home / "config.yaml")):
        record.update(status="suppressed", error=None)
    else:
        try:
            owner = find_canonical_live_owner(home)
        except ValueError:
            return False  # Owner not ready: discovery uncertainty is not a disposition.
        if owner is None:
            return False
        try:
            receipt = deliver_to_live_owner(
                home, owner, bot_chat_message(record.get("job") or {}, record["content"]),
                delivery_id=record["id"], notification_category="diagnostic" if for_failure else "result")
        except Exception as exc:
            # The door is idempotent per id (a replay returns the existing receipt), so the
            # record stays queued for the next tick; log loudly once, quietly after.
            logger.log(logging.DEBUG if path in _warned else logging.WARNING,
                       "Legacy Bot Chat pending record %s not handed off yet: %s", path, exc, exc_info=True)
            _warned.add(path)
            return False
        receipt_status = receipt.get("status") if isinstance(receipt, dict) else None
        record.update(status="transferred", error=None, receipt_status=receipt_status)
    return True


def _drain_one(path: Path) -> dict | None:
    from hermes_cli.active_sessions import _FileLock

    with _FileLock(path.with_suffix(".lock")):
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict):
            raise ValueError("pending delivery is not a JSON object")
        if record.get("status") not in {"queued", "transferred"}:
            return record  # A legacy claim or unknown turn is never replayed.
        if record.get("status") == "transferred" and not record.get("admission_id"):
            return record
        if _dispose(path, record):
            atomic_json_write(path, record, fsync_dir=True, mode=0o600)
        return record


def drain_pending(key: str) -> dict | None:
    """Process one exact receipt, serializing producers, drainers and direct callers."""
    return _drain_one(_pending_path(key))


def drain_legacy_pending() -> None:
    """Hand queued records to their owner and observe admitted wakes; never replay uncertain turns."""
    root = get_hermes_home().resolve() / "cron" / "bot_chat_pending"
    if not root.is_dir():
        return
    for path, _ in _queued(root):
        try:
            record = _drain_one(path)
        except Exception as exc:
            logger.log(logging.DEBUG if path in _warned else logging.WARNING,
                       "Legacy Bot Chat pending record %s left queued: %s", path, exc, exc_info=True)
            _warned.add(path)
            continue
        if record is not None:
            logger.info("Legacy Bot Chat pending record %s -> %s", path.name, record["status"])
