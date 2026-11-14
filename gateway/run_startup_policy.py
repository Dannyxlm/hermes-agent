"""Platform authorization policy declarations used during gateway startup."""

from typing import Optional


def _own_policy_envs(platform) -> Optional[tuple]:
    """``(dm_policy_env, group_policy_env, allow_all_env)`` for a platform that declares
    ``own_policy_env``, else None. The live entry wins; a bundled platform whose entry is absent
    (plugin disabled, safe mode) or lacks the field (a user plugin overriding it) falls back to the
    names its ``gateway_env.AUTHZ`` declares, so the startup refusal never weakens."""
    from gateway.platform_env import authz_field
    from gateway.platform_registry import platform_registry
    entry = platform_registry.get(platform.value)
    own = authz_field(platform.value, "own_policy_env", entry)
    if not own:
        return None
    return (*own, authz_field(platform.value, "allow_all_env", entry))
