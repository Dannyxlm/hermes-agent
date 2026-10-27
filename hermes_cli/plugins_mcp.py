"""Bounded native MCP envelopes for plugin consumers."""
from __future__ import annotations

import json
from typing import Any

_MARKER = "… [truncated]"


def _bounded(value: Any, cap: int) -> tuple[Any, bool]:
    """*value* within *cap* characters; an over-cap value becomes the head of its text plus a marker."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return (text[:cap] + _MARKER, True) if len(text) > cap else (value, False)


def _failure(parsed: dict) -> dict[str, Any]:
    """Error text and ``_meta`` pass the generic projection; ``structuredContent`` is the backend's own
    receipt and reaches the plugin as the server sent it."""
    from tools.mcp_tool_error_projection import GENERIC_ERROR, project_error_meta, project_error_text
    blocks = parsed["error"] if isinstance(parsed["error"], list) else [parsed["error"]]
    text = "".join(project_error_text(b if isinstance(b, str) else json.dumps(b, ensure_ascii=False, default=str))
                   for b in blocks)
    envelope: dict[str, Any] = {"ok": False, "error": text or GENERIC_ERROR}
    if "structuredContent" in parsed:
        envelope["structuredContent"] = parsed["structuredContent"]
    if "_meta" in parsed:
        envelope["_meta"] = project_error_meta(parsed["_meta"])
    return envelope


def _success(parsed: dict) -> dict[str, Any]:
    envelope: dict[str, Any] = {"ok": True, "result": parsed.get("result", parsed)}
    for key in ("structuredContent", "_meta"):
        if key in parsed:
            envelope[key] = parsed[key]
    return envelope


def mcp_envelope(raw: Any, cap: int) -> dict[str, Any]:
    """Parse the whole handler result, then bound each field: slicing the JSON first turned every large
    success into a failure. An object that cannot be cut and stay well-formed is dropped instead."""
    raw = raw if isinstance(raw, str) else "" if raw is None else str(raw)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if not isinstance(parsed, dict):
        envelope: dict[str, Any] = {"ok": True, "result": raw if parsed is None else parsed}
    else:
        envelope = _failure(parsed) if "error" in parsed else _success(parsed)
    truncated = isinstance(parsed, dict) and parsed.get("truncated") is True
    for key in ("result", "error"):
        if key in envelope:
            envelope[key], cut = _bounded(envelope[key], cap)
            truncated = truncated or cut
    for key in ("structuredContent", "_meta"):
        if key in envelope and _bounded(envelope[key], cap)[1]:
            del envelope[key]
            truncated = True
    return {**envelope, "truncated": True} if truncated else envelope
