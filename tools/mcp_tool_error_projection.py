"""Model-facing fields of MCP failures. Unknown diagnostics are not prompt data."""
from __future__ import annotations

import json
import re
from typing import Any

from tools.mcp_tool_common import _sanitize_error


_STATES = frozenset({"queued", "running", "recovering", "pending", "accepted", "committed",
                     "conflict", "failed", "cancelled", "held", "quarantined", "unknown"})
_NOTICES = frozenset({"pending", "degraded", "denied", "unavailable", "truncated", "held",
                      "quarantined", "retryable", "busy", "budget_exhausted", "held_files",
                      "search_degraded", "write_capacity_low"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_CODE = re.compile(r"[a-z][a-z_]{0,79}\Z")
_RECEIPT = {"request_id": _IDENTIFIER, "state": _STATES, "retry_after_ms": "retry",
            "revision": _IDENTIFIER, "compacted": bool,
            "persistence": {"mode": frozenset({"filesystem", "database"}), "file_written": bool,
                            "git_state": frozenset({"pending", "committed", "failed", "unverified", "clean", "dirty"})}}
_ERROR = {"error": _CODE, "code": _CODE, "write_error": _CODE, "retryable": bool,
          "retry_after_ms": "retry", "write_request": _RECEIPT}
_DROP = object()


def _project(value: Any, schema: Any) -> Any:
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            return _DROP
        result = {}
        for key, rule in schema.items():
            if key in value:
                item = _project(value[key], rule)
                if item is not _DROP:
                    result[key] = item
        return result
    if schema is bool:
        return value if type(value) is bool else _DROP
    if schema == "retry":
        return value if value is None or type(value) is int and 0 <= value <= 86400000 else _DROP
    if not isinstance(value, str) or _sanitize_error(value) != value:
        return _DROP
    if isinstance(schema, frozenset):
        return value if value in schema else _DROP
    return value if schema.fullmatch(value) else _DROP


def project_error_data(value: Any) -> dict:
    """Keep typed codes and receipt fields, never arbitrary nested server objects."""
    projected = _project(value, _ERROR)
    return projected if isinstance(projected, dict) else {}


def project_error_text(text: str) -> str:
    """A JSON text copy must pass the same projection as structuredContent."""
    try:
        value = json.loads(text)
    except ValueError:
        return "MCP tool returned an error" if text.lstrip().startswith(("{", "[")) else _sanitize_error(text)
    if isinstance(value, (dict, list)):
        return json.dumps(project_error_data(value), ensure_ascii=False)
    return _sanitize_error(text)


def project_error_meta(value: Any) -> dict:
    """Only bounded typed notices belong in failure metadata, not vendor/debug."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 128 or _sanitize_error(key) != key:
            continue
        if key not in {"vendor/notice", "vendor/warnings", "notices", "gbrain_notices"}:
            continue
        field = key.rsplit("/", 1)[-1]
        if field == "notice" and isinstance(item, str) and item in _NOTICES:
            result[key] = item
        elif field == "warnings" and isinstance(item, list):
            result[key] = [entry for entry in item[:16] if isinstance(entry, str) and entry in _NOTICES]
        elif field in {"notices", "gbrain_notices"} and isinstance(item, list):
            result[key] = [{"code": entry["code"]} for entry in item[:16]
                           if isinstance(entry, dict) and isinstance(entry.get("code"), str)
                           and entry["code"] in _NOTICES]
    return result
