"""Memory-provider schema gating, injection and collision-safe routing ownership."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def normalize_tool_schema(schema: Any) -> Optional[dict[str, Any]]:
    """Return a bare function-tool dict with a resolvable top-level ``name``, else None.

    Providers should return ``{"name", "description", "parameters"}`` but some return the
    wrapped OpenAI form; wrapping that twice yields a nameless ``function`` and strict
    providers (DeepSeek) reject the ENTIRE request, so both shapes are normalized here.
    """
    if not isinstance(schema, dict):
        return None
    if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
        schema = schema["function"]
    name = schema.get("name", "")
    return schema if name and isinstance(name, str) else None


def memory_provider_tools_enabled(enabled_toolsets: Optional[list[str]], disabled_toolsets: Optional[list[str]] = None,
                                  *, memory_tool_present: bool = False) -> bool:
    """Return whether external memory-provider tools should be exposed."""
    if disabled_toolsets and "memory" in disabled_toolsets:
        return False
    if enabled_toolsets is not None and not enabled_toolsets:
        return False
    if memory_tool_present or enabled_toolsets is None:
        return True
    if not enabled_toolsets:
        return False
    if "memory" in enabled_toolsets:
        return True
    try:
        from toolsets import resolve_toolset

        return any("memory" in resolve_toolset(name) for name in enabled_toolsets)
    except Exception:
        logger.debug("Failed to resolve enabled toolsets for memory-provider tools", exc_info=True)
        return False


def _tool_name(tool: Any) -> Any:
    return tool.get("function", {}).get("name") if isinstance(tool, dict) else None


def memory_provider_tools_exposed(agent: Any) -> bool:
    """Whether external memory-provider tools are exposed on ``agent``.

    Same gate as ``inject_memory_provider_tools`` so a provider's ``system_prompt_block()``
    never advertises tools absent from the tool surface.
    """
    tools = getattr(agent, "tools", None)
    present = isinstance(tools, (list, tuple)) and any(_tool_name(t) == "memory" for t in tools)
    enabled, disabled = getattr(agent, "enabled_toolsets", None), getattr(agent, "disabled_toolsets", None)
    return memory_provider_tools_enabled(enabled, disabled, memory_tool_present=present)


def inject_memory_provider_tools(agent: Any) -> int:
    """Append external memory-provider tool schemas to an agent tool surface."""
    memory_manager = getattr(agent, "_memory_manager", None)
    tools = getattr(agent, "tools", None)
    if not memory_manager or tools is None:
        return 0

    existing_tool_names = {
        tool.get("function", {}).get("name")
        for tool in tools
        if isinstance(tool, dict)
    }
    previous_provider_names = set(
        getattr(agent, "_memory_provider_tool_names", set()) or set()
    )
    if not memory_provider_tools_exposed(agent):
        # A provider is configured but the memory toolset is gated off
        # (platform_toolsets / disabled_toolsets). Say so once — a silent
        # return 0 here made #81014 undiagnosable: the provider looked
        # "half on" with no clue which config key suppressed its tools.
        _providers = [
            p for p in (getattr(memory_manager, "providers", None) or [])
            if getattr(p, "name", "") != "builtin"
        ]
        if _providers:
            logger.info(
                "Memory provider(s) %s configured but the 'memory' toolset is "
                "gated off for this session (platform_toolsets / "
                "agent.disabled_toolsets) — provider tools and system-prompt "
                "block are both withheld.",
                [getattr(p, "name", type(p).__name__) for p in _providers],
            )
        agent._memory_provider_tool_names = set()
        return 0

    get_schemas = getattr(memory_manager, "get_all_tool_schemas", None)
    if not callable(get_schemas):
        return 0

    valid_tool_names = getattr(agent, "valid_tool_names", None)
    if valid_tool_names is None:
        valid_tool_names = set()
        agent.valid_tool_names = valid_tool_names

    added = 0
    provider_tool_names = set()
    for raw_schema in get_schemas():
        schema = normalize_tool_schema(raw_schema)
        if schema is None:
            logger.warning(
                "Memory provider returned a tool schema with no resolvable "
                "name; skipping to avoid poisoning the request (%r)",
                raw_schema,
            )
            continue
        tool_name = schema["name"]
        if tool_name in existing_tool_names:
            if tool_name in previous_provider_names:
                provider_tool_names.add(tool_name)
            continue
        tools.append({"type": "function", "function": schema})
        valid_tool_names.add(tool_name)
        existing_tool_names.add(tool_name)
        provider_tool_names.add(tool_name)
        added += 1

    agent._memory_provider_tool_names = provider_tool_names
    return added


def memory_provider_owns_tool(agent: Any, tool_name: str) -> bool:
    """Return whether this agent advertised ``tool_name`` from its provider."""
    memory_manager = getattr(agent, "_memory_manager", None)
    if not memory_manager:
        return False
    provider_tool_names = getattr(agent, "_memory_provider_tool_names", None)
    if provider_tool_names is None:
        # Backward compatibility for lightweight embedders/test doubles that
        # predate agent-level routing ownership. Real AIAgent instances always
        # initialize the set before provider schema injection.
        return memory_manager.has_tool(tool_name)
    return bool(
        tool_name in provider_tool_names
        and memory_manager.has_tool(tool_name)
    )
