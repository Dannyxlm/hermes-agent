"""Let a tool write a new call's arguments in the form every reader of the call expects.

Runs on the model's *new* assistant turn, right after ``expand_local_tool_batches`` and before it is
de-duplicated, staged, persisted or shown (``turn_tool_round``): never an earlier message, so no cached
prefix moves. Everyone downstream (``tool.start`` / ``tool.complete``, the stored turn, history replay,
the model's next request) then reads one set of arguments, including the native transports that replay
ordered content blocks instead of rebuilding them from ``tool_calls`` (patched here as the batch expansion
patches them). A tool opts in with ``registry.register(normalize_args=...)``; that writer returns its
``args`` unchanged (the same object) when there is nothing to write.
"""

import json
import logging

from agent.message_sanitization import coalesce_tool_call_id
from tools.registry import registry

logger = logging.getLogger(__name__)


def write_registered_arguments(tool_calls: list, *, provider_data: dict | None = None) -> None:
    """Rewrite, in place, each call whose tool registered a ``normalize_args`` writer and changed them."""
    written = {}
    for call in tool_calls:
        normalize = registry.get_args_normalizer(call.function.name)
        if normalize is None:
            continue
        try:
            args = json.loads(call.function.arguments)
            new = normalize(args) if isinstance(args, dict) else args
            text = json.dumps(new, ensure_ascii=False) if new is not args else None
        except Exception as exc:  # health: allow BLE001 -- tool code in the turn loop: the call goes on as sent
            logger.warning("argument writer for %s failed (%s); the call goes on as sent",
                           call.function.name, type(exc).__name__)
            continue
        if text is not None:
            call.function.arguments = text
            written[coalesce_tool_call_id(call)] = new
    if provider_data and written:
        _rewrite_native_blocks(provider_data, written)


def _rewrite_native_blocks(provider_data: dict, written: dict) -> None:
    """Anthropic and Bedrock Converse replay the turn from ordered block sidecars: give their tool blocks
    the written input too, keeping every other block (signed thinking, text) and the order exactly."""
    if blocks := provider_data.get("anthropic_content_blocks"):
        provider_data["anthropic_content_blocks"] = [
            {**block, "input": written[block.get("id")]}
            if block.get("type") == "tool_use" and block.get("id") in written else block for block in blocks]
    if blocks := provider_data.get("bedrock_content_blocks"):
        provider_data["bedrock_content_blocks"] = [
            {**block, "toolUse": {**block["toolUse"], "input": written[block["toolUse"].get("toolUseId")]}}
            if isinstance(block.get("toolUse"), dict) and block["toolUse"].get("toolUseId") in written else block
            for block in blocks]
