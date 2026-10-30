"""Profile-aware tool selection for gateway agent construction."""
import logging

from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


def resolve_enabled_toolsets_for_source(
    self, user_config: dict, source: SessionSource, platform_key: str,
) -> list:
    """Enabled toolsets for an agent run, honoring an adapter ``toolsets_for_source()`` override
    validated through the SAME ``_get_platform_tools`` path (unknown / platform-restricted
    toolsets dropped, not trusted)."""
    from gateway.guest_channels import guest_turn_toolsets
    guest_tools = guest_turn_toolsets(self, source, user_config)
    if guest_tools is not None:
        return guest_tools
    from hermes_cli.tools_config import _get_platform_tools
    try:
        adapter = self._delivery_adapter_for(source)
        override = adapter.toolsets_for_source(source) if adapter is not None else None
    except Exception:
        logger.warning("Adapter toolset override failed; using profile configuration", exc_info=True)
        override = None
    if override and isinstance(override, list):
        pts = dict(user_config.get("platform_toolsets") or {})
        pts[platform_key] = [str(x) for x in override]
        user_config = {**user_config, "platform_toolsets": pts}
    return sorted(_get_platform_tools(user_config, platform_key))
