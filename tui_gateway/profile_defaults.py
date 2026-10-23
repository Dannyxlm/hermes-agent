"""Semantic writes for new-chat defaults, using the existing dashboard writer/lock.
No agent construction, session mutation, environment changes or credential writes.
"""
import json
import os
import stat
import time
import uuid

from hermes_cli.config import atomic_config_write, read_user_config_raw
from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort


def read_defaults(path):
    cfg = read_user_config_raw(path) if path.exists() else {}
    agent, delegation, model = (cfg.get(k) or {} for k in ("agent", "delegation", "model"))
    parsed = parse_reasoning_effort(agent.get("reasoning_effort", "medium")) or {}
    reasoning = "none" if parsed.get("enabled") is False else parsed.get("effort", "medium")
    return {"reasoning": reasoning, "reasoning_levels": ["none", *VALID_REASONING_EFFORTS],
            "delegation_model": delegation.get("model") or "",
            "delegation_provider": delegation.get("provider") or "",
            "main_model": model.get("default", "") if isinstance(model, dict) else model,
            "applies_to": "new_chats_and_delegations", "reasoning_precedence": "session > per-model > profile default"}


def validate_default(key, value, provider):
    if key == "reasoning":
        if value not in {"none", *VALID_REASONING_EFFORTS}:
            raise ValueError("unsupported reasoning level")
        return
    if not value and not provider:  # empty means inherit the parent's model
        return
    from hermes_cli.models_validate import validate_requested_model
    from providers import get_provider_profile
    if not provider or get_provider_profile(provider) is None:
        raise ValueError("unknown provider")
    verdict = validate_requested_model(value, provider)
    if not (verdict["accepted"] and verdict["persist"] and verdict["recognized"]):
        raise ValueError("model is not in the server's supported provider catalog")


def _audit(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("defaults audit must be a regular file")
        # The creation mode does not tighten a restored/pre-existing audit.
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_default(path, *, key, value, provider, principal):
    # Same lock as /api/model/set and dashboard config autosave. Catalog/network
    # validation has already finished outside the lock, as in the main-model writer.
    from hermes_cli.web_server import _CONFIG_MUTATION_LOCK
    with _CONFIG_MUTATION_LOCK:
        before = read_defaults(path)
        same = (before["reasoning"] == value if key == "reasoning" else
                (before["delegation_model"], before["delegation_provider"]) == (value, provider))
        if same:
            return {**before, "changed": False, "operation_id": None}
        cfg = read_user_config_raw(path) if path.exists() else {}
        if key == "reasoning":
            cfg.setdefault("agent", {})["reasoning_effort"] = value
        else:
            delegation = cfg.setdefault("delegation", {})
            if delegation.get("base_url") or delegation.get("api_key"):
                raise ValueError("direct delegation endpoint configured; use full settings to change its route")
            delegation.update(model=value, provider=provider)
        operation_id = uuid.uuid4().hex
        audit_path = path.parent / "logs" / "defaults-audit.jsonl"
        fields = {"operation_id": operation_id, "at": time.time(), "principal": principal,
                  "key": key, "value": value, "provider": provider,
                  "previous": before["reasoning"] if key == "reasoning" else
                              {"model": before["delegation_model"], "provider": before["delegation_provider"]}}
        _audit(audit_path, {**fields, "status": "intent"})
        atomic_config_write(path, cfg)
        after = read_defaults(path)
        matches = (after["reasoning"] == value if key == "reasoning" else
                   (after["delegation_model"], after["delegation_provider"]) == (value, provider))
        if not matches:
            raise OSError("default config readback did not match")
        _audit(audit_path, {**fields, "status": "applied"})
        return {**after, "changed": True, "operation_id": operation_id}
