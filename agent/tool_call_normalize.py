"""Let a tool write a new call's arguments in the form every reader of the call expects.

Runs on the model's *new* assistant turn, after validation and before it is staged, persisted or
shown (``turn_tool_round``), the same window ``expand_local_tool_batches`` uses: never an earlier
message, so no cached prefix moves. Everyone downstream (``tool.start`` / ``tool.complete``, the stored
turn, history replay, the model's next request) then reads one set of arguments. A tool opts in with
``registry.register(normalize_args=...)``; that writer returns its ``args`` unchanged (the same object)
when there is nothing to write.
"""

import json

from tools.registry import registry


def write_registered_arguments(tool_calls: list) -> None:
    """Rewrite, in place, each call whose tool registered a ``normalize_args`` writer and changed them."""
    for call in tool_calls:
        normalize = registry.get_args_normalizer(call.function.name)
        if normalize is None:
            continue
        try:
            args = json.loads(call.function.arguments)
        except (TypeError, ValueError):  # left for the dispatcher's own error
            continue
        if not isinstance(args, dict):
            continue
        written = normalize(args)
        if written is not args:
            call.function.arguments = json.dumps(written, ensure_ascii=False)
