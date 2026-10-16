"""Bounded native MCP envelopes for plugin consumers."""
from __future__ import annotations

import json
from typing import Any


def mcp_envelope(raw: Any, cap: int) -> dict[str, Any]:
    """Keep native metadata and typed errors; never parse an incomplete JSON document."""
    raw = raw if isinstance(raw, str) else "" if raw is None else str(raw)
    if len(raw) > cap:
        # Bound parsing too. Handler JSON is object-shaped; slicing it could turn a
        # native denial into a successful string. Plain text keeps the legacy cap.
        if raw.lstrip().startswith(("{", "[")):
            return {"ok": False, "error": "mcp_result_too_large", "truncated": True}
        return {"ok": True, "result": raw[:cap] + "… [truncated]", "truncated": True}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if not isinstance(parsed, dict):
        return {"ok": True, "result": parsed if parsed is not None else raw}
    if "error" in parsed:
        envelope = {"ok": False, "error": parsed["error"]}
    elif "result" in parsed:
        envelope = {"ok": True, "result": parsed["result"]}
    else:
        envelope = {"ok": True, "result": parsed}
    for key in ("structuredContent", "_meta", "truncated"):
        if key in parsed:
            envelope[key] = parsed[key]
    return envelope
