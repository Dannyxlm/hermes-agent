"""Credential-pool selection strategies and their read-only config seam."""
STRATEGY_FILL_FIRST = "fill_first"
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGY_RANDOM = "random"
STRATEGY_LEAST_USED = "least_used"
STRATEGY_SMART = "smart"
SUPPORTED_POOL_STRATEGIES = {
    STRATEGY_FILL_FIRST, STRATEGY_ROUND_ROBIN, STRATEGY_RANDOM, STRATEGY_LEAST_USED, STRATEGY_SMART,
}


def get_pool_strategy(provider: str) -> str:
    from agent.credential_pool import _load_config_safe
    config = _load_config_safe()
    strategies = config.get("credential_pool_strategies") if config else None
    if not isinstance(strategies, dict):
        return STRATEGY_FILL_FIRST
    strategy = str(strategies.get(provider, "") or "").strip().lower()
    return strategy if strategy in SUPPORTED_POOL_STRATEGIES else STRATEGY_FILL_FIRST
