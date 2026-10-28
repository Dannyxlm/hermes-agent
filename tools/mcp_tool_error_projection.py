"""Generic projection of MCP failures for plugin consumers (``ctx.call_mcp``).

A plugin may forward a failure into a prompt, so its error text and failure ``_meta`` keep only typed
fields: a code-shaped ``error``/``code``, ``retryable``, ``retry_after_ms`` and notice codes under any
namespace's ``notice``/``warnings``/``notices`` key. Codes are checked by shape, never by a vocabulary,
so no backend's names live here; a backend's own receipt stays in the unprojected ``structuredContent``
for its plugin to read. The model-facing renderer does not use this: it shows the server's sanitized text.
"""
from __future__ import annotations

import json
import re
from typing import Any

from tools.mcp_tool_common import _sanitize_error

GENERIC_ERROR = "MCP tool returned an error"
_CODE = re.compile(r"[a-z][a-z_]{0,79}\Z")
_NOTICE_LIMIT = 16


def _is_code(value: Any) -> bool:
    return isinstance(value, str) and _CODE.fullmatch(value) is not None and _sanitize_error(value) == value


def _is_retry_after(value: Any) -> bool:
    return value is None or type(value) is int and 0 <= value <= 86_400_000


_FIELDS = {"error": _is_code, "code": _is_code, "retryable": lambda value: type(value) is bool,
           "retry_after_ms": _is_retry_after}


def project_error_data(value: Any) -> dict:
    """The typed failure fields of *value*; everything else is server diagnostics."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key, valid in _FIELDS.items() if key in value and valid(value[key])}


def project_error_text(text: str) -> str:
    """A JSON error body passes the same projection as structured data; prose is sanitized."""
    try:
        value = json.loads(text)
    except ValueError:
        return GENERIC_ERROR if text.lstrip().startswith(("{", "[")) else _sanitize_error(text)
    if isinstance(value, (dict, list)):
        projected = project_error_data(value)
        return json.dumps(projected, ensure_ascii=False) if projected else GENERIC_ERROR
    return _sanitize_error(text)


def _notice_codes(field: str, item: Any) -> Any:
    if field == "notice":
        return item if _is_code(item) else None
    if not isinstance(item, list):
        return None
    if field == "warnings":
        return [entry for entry in item[:_NOTICE_LIMIT] if _is_code(entry)]
    return [{"code": entry["code"]} for entry in item[:_NOTICE_LIMIT]
            if isinstance(entry, dict) and _is_code(entry.get("code"))]


def project_error_meta(value: Any) -> dict:
    """Notice codes from failure metadata; other keys (debug dumps, traces) are dropped."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 128 or _sanitize_error(key) != key:
            continue
        field = key.rsplit("/", 1)[-1]
        if field in ("notice", "warnings", "notices") and (codes := _notice_codes(field, item)) is not None:
            result[key] = codes
    return result
