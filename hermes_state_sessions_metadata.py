"""Pure topical helpers extracted without behavior changes for the file-size ratchet."""

import json
from typing import Any, Optional
from hermes_state_common import _sql_json_extract


def workspace_key(row: dict[str, Any]) -> Optional[str]:
    """Workspace grouping key: git repo root, else cwd, else None (branch excluded: a checkout must not
    fragment history)."""
    return (row.get("git_repo_root") or "").strip() or (row.get("cwd") or "").strip() or None


def _delegate_from_json(col: str = "model_config") -> str:
    return _sql_json_extract(col, "$._delegate_from")


def _parse_model_config(raw: Any) -> dict[str, Any]:
    """Tolerant ``model_config`` decode: JSON text or dict -> dict copy; anything else -> {}."""
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _where_sql(clauses: list[str], lead: str = "") -> str:
    """``WHERE a AND b`` (with *lead* prefix) or "" when there are no clauses."""
    return f"{lead}WHERE {' AND '.join(clauses)}" if clauses else ""
