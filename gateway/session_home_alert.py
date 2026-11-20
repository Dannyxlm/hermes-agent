"""A Bot Chat reply that needs the user alerts their phone; every other reply only marks it unread.

Replies a viewer's own turn produces already reach the phone through the turn's push projection
(``tui_gateway/methods_mobile_push.py``). Turns the gateway runs for a producer (a scheduled
job's report, a teammate's DM, a background completion) have no such projection, so the phone
only sees them as new messages. A producer turn's reply that opens with ``NEEDS_YOU`` is the one
exception: it records one completed run on the Bot Chat's canonical push scope, which alerts every
device holding that chat's alert lease, under the device's own preview and category choices.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

NEEDS_YOU = "[NEEDS YOU]"


def needs_you_body(content) -> str | None:
    """The reply without its leading ``[NEEDS YOU]`` marker, or None when it carries none.

    Only a marker that opens the reply counts (case-insensitive); one quoted later is prose."""
    if not isinstance(content, str):
        return None
    text = content.lstrip()
    if text[:len(NEEDS_YOU)].upper() != NEEDS_YOU:
        return None
    return text[len(NEEDS_YOU):].strip()


def _canonical_root(db, chat_id) -> str | None:
    """The canonical Bot Chat root when ``chat_id`` is that chat (its root or a compression tip)."""
    from hermes_state import SessionDB
    row = db.get_session_by_title(SessionDB.CANONICAL_BOT_CHAT_TITLE)
    if not row or row.get("archived"):
        return None
    chain = db.get_compression_chain(row["id"]) or [row["id"]]
    return row["id"] if chat_id in chain else None


def _profile(home: Path):
    from hermes_cli.profiles import list_profiles
    home = home.resolve()
    return next((p for p in list_profiles(lazy_skill_count=True) if Path(p.path).resolve() == home), None)


def push_service():
    """The process's one outbox, as the mobile RPCs use it: the launch home's, for every profile."""
    import os
    import gateway.run
    from tui_gateway import mobile_push
    if os.environ.get("HERMES_COMPUTE_HOST_CHILD") == "1":
        return None  # the supervisor owns delivery, as in methods_mobile_push._mobile_push_service
    return mobile_push.service_for_home(gateway.run._hermes_home)


def record_needs_you(db, chat_id, body) -> bool:
    """Record the alert run for ``chat_id``'s reply; False when nothing was recorded."""
    from tui_gateway.mobile_push_payloads import AlertPreview, Scope, agent_name
    root = _canonical_root(db, chat_id)
    if root is None:
        return False
    service = push_service()
    profile = _profile(Path(db.db_path).parent) if service is not None else None
    if profile is None:
        return False
    # A profile with no presentation name is titled by its agent ("default" is Ava), never its id.
    title = profile.bot_title or profile.display_name or agent_name(profile.name)
    preview = AlertPreview(title=title, reply=body)
    scope = Scope("native", profile.name, root)
    run = service.start_run(scope, preview=preview)
    return service.record(scope, run["run_id"], "needs-you", "complete", preview=preview)


async def notify_if_needed(authority, chat_id, content) -> None:
    """Best effort: a failed projection never fails the reply's delivery."""
    body = needs_you_body(content)
    if body is None or authority is None or getattr(authority, "db", None) is None:
        return
    try:
        await asyncio.to_thread(record_needs_you, authority.db, chat_id, body)
    except Exception as error:  # health: allow BLE001 -- projection only; exception text can carry private paths
        logger.warning("Bot Chat needs-you alert unavailable (%s)", type(error).__name__)
