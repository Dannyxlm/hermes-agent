"""Explicit, route-bound guest admission for a shared Slack bot.

A channel allowlist is deliberately NOT a guest grant: it authorizes the full
owner agent even if a route is removed. Guest admission pins both the workspace
and the restricted runtime home before bypassing the sender allowlist.
"""
from __future__ import annotations

import logging

import yaml

from agent.i18n import t

from gateway.config import Platform
from gateway.session_identity import identity_of

logger = logging.getLogger(__name__)


def guest_channel_policy(runner, source):
    """Return the receiving bot's policy for this workspace/channel, or None.

    Missing workspace provenance must not turn a configured guest channel into
    ordinary owner traffic. A matching channel suffix with no scope is denied.
    """
    if getattr(source, "platform", None) != Platform.SLACK or getattr(source, "chat_type", None) != "group":
        return None
    adapter = runner._intake_adapter_for(source)
    extra = getattr(getattr(adapter, "config", None), "extra", {}) or {}
    channels = extra.get("guest_channels", {})
    if not isinstance(channels, dict):
        return None
    scope = getattr(source, "scope_id", None)
    chat_id = getattr(source, "chat_id", "") or ""
    if not scope:
        if chat_id and any(isinstance(key, str) and key.endswith(":" + chat_id) for key in channels):
            logger.warning("Slack guest-channel event lacks team_id; denying without workspace provenance")
            return {}
        return None
    return channels.get(f"{scope}:{chat_id}")


def is_guest_source(runner, source):
    policy = guest_channel_policy(runner, source)
    return policy is not None and (
        not isinstance(policy, dict) or not getattr(source, "user_id", None)
        or getattr(source, "user_id", None) != policy.get("owner_user_id"))


def guest_channel_authorized(runner, source):
    """Tri-state verdict: None keeps existing authorization; False never falls back.

    Even an otherwise allowlisted non-owner must run on the configured guest
    profile. Read its config strictly, not via the gateway's {}-on-error loader.
    """
    if not is_guest_source(runner, source):
        return None
    policy = guest_channel_policy(runner, source)
    if not isinstance(policy, dict) or not getattr(source, "user_id", None) or getattr(source, "is_bot", False):
        return False
    identity = identity_of(source)
    target = policy.get("profile")
    if (not target or target == "default" or identity is None or not identity.multiplexed
            or identity.runtime_profile != target or getattr(source, "profile_route_rejected", False)):
        return False
    from hermes_cli.profiles import get_profile_dir
    try:
        expected = get_profile_dir(target).resolve()
        if identity.runtime_home.resolve() != expected or not expected.is_dir():
            return False
        config = yaml.safe_load((expected / "config.yaml").read_text(encoding="utf-8-sig"))
        memory = config.get("memory", {})
        return (config.get("platform_toolsets", {}).get("slack") == []
                and memory.get("memory_enabled") is False
                and memory.get("user_profile_enabled") is False
                and memory.get("provider", "") in ("", "none")
                and not config.get("mcp_servers")
                and config.get("skills", {}).get("external_dirs") == []
                and config.get("plugins", {}).get("enabled") == [])
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return False


def guest_turn_toolsets(runner, source, config):
    """Keep the actual agent construction fail-closed after admission, too."""
    if not is_guest_source(runner, source):
        return None
    if guest_channel_authorized(runner, source) is not True:
        raise PermissionError("Guest profile is unavailable or unrestricted")
    if config.get("platform_toolsets", {}).get("slack") != []:
        raise PermissionError("Guest turn config could not be loaded safely")
    return []


def guest_slash_denial(runner, source):
    if not is_guest_source(runner, source):
        return None
    policy = guest_channel_policy(runner, source)
    override = policy.get("slash_denied") if isinstance(policy, dict) else None
    return override if isinstance(override, str) and override.strip() else t("gateway.guest.slash_denied")


def restrict_guest_context(runner, context):
    """Remove owner infrastructure before tools/env or the prompt pin can see it."""
    if not is_guest_source(runner, context.source):
        return context
    from dataclasses import replace
    return replace(context, connected_platforms=[], home_channels={}, guest_restricted=True)
