"""Transport the assigned profile's provider-tier policy to one worker.

The private env key is a spawn-only config mirror, not a user-facing setting.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TIER_ENV = "_HERMES_KANBAN_SERVICE_TIER"


def set_worker_service_tier(env, task, hermes_home):
    """Scrub inherited policy and pin only this worker's mapped provider tier."""
    from agent.fast_mode import parse_exact_service_tier
    from hermes_cli.config import load_config_readonly, split_model_config_default
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope

    env.pop(_TIER_ENV, None)
    if not hermes_home:
        return
    with _worker_profile_scope(hermes_home):
        config = load_config_readonly()
    tiers = (config.get("kanban") or {}).get("service_tier_by_provider") or {}
    if not isinstance(tiers, dict):
        logger.warning("kanban.service_tier_by_provider must be a provider-to-tier mapping; ignoring")
        return
    model = config.get("model") or {}
    _, nested_provider = split_model_config_default(model.get("default") or model.get("model"))
    provider = task.provider_override or nested_provider or model.get("provider")
    if provider not in tiers:
        return
    raw = tiers[provider]
    try:
        if not isinstance(raw, str):
            raise ValueError("tier must be a string")
        env[_TIER_ENV] = parse_exact_service_tier(raw) or "normal"
    except ValueError:
        logger.warning("Invalid kanban.service_tier_by_provider tier for %s; ignoring", provider)


def apply_worker_service_tier(config):
    """Apply the spawn default in memory, below explicit/global and managed tiers."""
    from agent.delegation_context import owned_kanban_task

    configured = config["agent"].get("service_tier")
    if not str(configured or "").strip() and owned_kanban_task() and _TIER_ENV in os.environ:
        config["agent"]["service_tier"] = os.environ[_TIER_ENV]
