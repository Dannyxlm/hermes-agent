"""Explicit, route-bound guest admission for a shared Slack bot.

A channel allowlist is deliberately NOT a guest grant: it authorizes the full
owner agent even if a route is removed. Guest admission pins both the workspace
and the restricted runtime home before bypassing the sender allowlist.
"""
from __future__ import annotations

import yaml

from gateway.config import Platform
from gateway.session_identity import identity_of


def guest_channel_policy(runner, source):
    """Return the receiving bot's policy for this workspace/channel, or None.

    Missing workspace provenance must not turn a configured guest channel into
    ordinary owner traffic. A matching channel suffix with no scope is denied.
    """
    if source.platform != Platform.SLACK or source.chat_type != "group":
        return None
    adapter = runner._intake_adapter_for(source)
    extra = getattr(getattr(adapter, "config", None), "extra", {}) or {}
    channels = extra.get("guest_channels", {})
    if not isinstance(channels, dict):
        return None
    scope = source.scope_id
    if not scope:
        return {} if any(key.endswith(":" + source.chat_id) for key in channels) else None
    return channels.get(f"{scope}:{source.chat_id}")


def is_guest_source(runner, source):
    policy = guest_channel_policy(runner, source)
    return policy is not None and (
        not isinstance(policy, dict) or not source.user_id
        or source.user_id != policy.get("owner_user_id"))


def guest_channel_authorized(runner, source):
    """Tri-state verdict: None keeps existing authorization; False never falls back.

    Even an otherwise allowlisted non-owner must run on the configured guest
    profile. Read its config strictly, not via the gateway's {}-on-error loader.
    """
    if not is_guest_source(runner, source):
        return None
    policy = guest_channel_policy(runner, source)
    if not isinstance(policy, dict) or not source.user_id or source.is_bot:
        return False
    identity = identity_of(source)
    target = policy.get("profile")
    if (not target or target == "default" or identity is None or not identity.multiplexed
            or identity.runtime_profile != target or source.profile_route_rejected):
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
