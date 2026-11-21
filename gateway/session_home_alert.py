"""A Bot Chat reply that needs the user alerts their phone; every other reply only marks it unread.

Replies to a viewer's own prompt already reach the phone through the turn's push projection
(``tui_gateway/methods_mobile_push.py``). Turns the gateway runs for a producer (a scheduled job's
report, a teammate's DM, a background completion) have no such projection, so the phone only sees
them as new messages. A producer turn's final reply that opens with ``NEEDS_YOU`` is the one
exception: it records one completed run on the Bot Chat's canonical push scope, alerting as
*attention* every device holding that chat's alert lease, under the device's own preview choice.
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


def _canonical_root(store, session_id) -> str | None:
    """The canonical Bot Chat's root when ``session_id`` is that chat's current tip.

    The identity the mobile roster and push scope use (``methods_mobile._mobile_canonical_identity``):
    one titled, unarchived root whose compression chain ends at an unarchived tip. A reset, branched
    or retitled session is not Home, and an ambiguous title is no one's."""
    from persistence.conversation.sessions import CANONICAL_BOT_CHAT_TITLE

    root = store.sessions.get_by_title(CANONICAL_BOT_CHAT_TITLE)
    if root is None or root.archived:
        return None
    root_id = root.session_id
    if store.sessions.title_holder(CANONICAL_BOT_CHAT_TITLE, excluding=root_id) is not None:
        return None
    chain = store.lineage.lineage(root_id)
    if not chain or chain[0] != root_id or chain[-1] != session_id:
        return None
    tip = store.sessions.get(chain[-1])
    return root_id if tip is not None and not tip.archived else None


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


def record_needs_you(authority, ref, body) -> bool:
    """Record the alert run for the reply the turn on ``ref`` gave; False when nothing was recorded."""
    from gateway.session_persistence import authority_persistence
    from tui_gateway.mobile_push_payloads import AlertPreview, Scope, agent_name

    store = authority_persistence(authority)
    # The physical session the turn ended in: its compression tip, after any mid-turn compression.
    root = _canonical_root(store, authority.physical_target(ref))
    if root is None:
        return False
    service = push_service()
    profile = _profile(store.scope.profile_dir) if service is not None else None
    if profile is None:
        return False
    # A profile with no presentation name is titled by its agent ("default" is Ava), never its id.
    title = profile.bot_title or profile.display_name or agent_name(profile.name)
    preview = AlertPreview(title=title, reply=body)
    scope = Scope("native", profile.name, root)
    run = service.start_run(scope, preview=preview, listed=True)
    return service.record(scope, run["run_id"], "needs-you", "complete", preview=preview, category="attention")


async def notify_if_needed(authority, ref, content) -> None:
    """Best effort, for a producer turn's final reply: a failed projection never fails the turn."""
    body = needs_you_body(content)
    if body is None or getattr(authority, "persistence", None) is None:
        return
    try:
        await asyncio.to_thread(record_needs_you, authority, ref, body)
    except Exception as error:  # health: allow BLE001 -- projection only; exception text can carry private paths
        logger.warning("Bot Chat needs-you alert unavailable (%s)", type(error).__name__)
