"""Slack early authorization, before enrichment or attachment downloads."""
import logging
from typing import Optional

logger = logging.getLogger("plugins.platforms.slack.adapter")


def early_reject_unauthorized(
        self, user_id: str, channel_id: str, is_dm: bool, *, thread_id: Optional[str] = None,
        team_id: Optional[str] = None) -> bool:
    """True (logged) when the sender is definitively unauthorized. Injected profile-bound check
    first (works under multiplex, where the handler has no ``__self__``), then runner
    introspection. Unknown (None) is NOT a rejection. ``thread_id``: thread-scoped profile_routes."""
    chat_type = "dm" if is_dm else "group"
    decision = (
        self._is_sender_authorized(user_id, chat_type, channel_id, thread_id=thread_id, scope_id=team_id)
        if user_id and getattr(self, "_authorization_check", None) is not None
        else None)
    auth_fn = self._runner_auth_fn()
    if decision is None and user_id and callable(auth_fn):
        source = self.build_source(
            chat_id=channel_id, chat_name="", chat_type=chat_type, user_id=user_id, user_name="",
            thread_id=thread_id, scope_id=team_id)
        decision = bool(auth_fn(source))
    if decision is False:
        logger.warning(
            "[Slack] Early reject of unauthorized user %s in channel %s", user_id, channel_id)
    return decision is False
