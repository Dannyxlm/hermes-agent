"""Opt-in transcript display projection; durable/provider replay data is never changed."""

import json


# The default /messages route remains the complete inspection/export surface.
# Display clients opt in to these transcript and presentation fields so future
# model replay columns cannot silently multiply every phone history response.
_DISPLAY_FIELDS = frozenset({
    "id", "row_id", "message_uid", "session_id", "role", "content", "timestamp",
    "tool_calls", "tool_call_id", "tool_name", "tool_use_id", "name",
    "display_kind", "display_content", "display_metadata", "tool_call_labels",
    "reasoning", "reasoning_content", "display_reasoning", "reasoning_details",
    "attachments", "source", "_source", "_ts", "_turnTps", "token_count",
    "finish_reason", "effect_disposition", "platform_message_id", "message_id",
    "observed", "active", "compacted", "_compressed_summary",
    "context", "args", "args_text", "labels", "summary", "preview", "result",
    "result_text", "inline_diff", "tool_result_metadata", "duration_s", "status",
})
_REASONING_FIELDS = frozenset({"type", "text", "summary", "thinking", "content"})


def _reasoning_display(value, *, serialized: bool = False):
    """Keep readable detail blocks, excluding signed/encrypted provider envelopes."""
    if isinstance(value, str):
        if not serialized:
            return value
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        return _reasoning_display(decoded) if isinstance(decoded, (list, dict)) else value
    if isinstance(value, list):
        return [projected for item in value if (projected := _reasoning_display(item)) is not None]
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str) and kind in {"reasoning.encrypted", "redacted_thinking", "compaction"}:
            return None
        return {key: projected for key, item in value.items() if key in _REASONING_FIELDS
                and (projected := _reasoning_display(item)) is not None}
    return None


def project_display_rows(messages: list[dict]) -> list[dict]:
    rows = []
    for message in messages:
        row = {key: value for key, value in message.items() if key in _DISPLAY_FIELDS}
        if row.get("reasoning_details") is not None:
            row["reasoning_details"] = _reasoning_display(row["reasoning_details"], serialized=True)
        rows.append(row)
    return rows
