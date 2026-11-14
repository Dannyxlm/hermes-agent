"""Plugin activation summaries and listener state for the tools RPC handlers."""

from .method_ctx import bind_module


# Latest ``on_plugin_loaded`` summaries by plugin name — the TUI server's own subscription, so an
# install/toggle/update result reports what the load actually activated (#87770). One listener per manager.
_plugin_activations: dict = {}
_plugin_activation_subscribed: set = set()


def _ensure_plugin_activation_listener() -> None:
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    if manager.scope_key in _plugin_activation_subscribed:
        return
    _plugin_activation_subscribed.add(manager.scope_key)

    def _on_loaded(summaries) -> None:
        for entry in summaries:
            _plugin_activations[entry["name"]] = entry
            _plugin_activations[entry["key"]] = entry
    manager.on_plugin_loaded(_on_loaded)


def _with_activation(result: dict, name: str) -> dict:
    """Fill ``activation`` from this process's listener when the core returned none (the core's own
    copy carries ``live_now``, which the listener's load-time summary cannot)."""
    if result.get("activation"):
        return result
    for key in (name, result.get("plugin_name"), result.get("name")):
        if key and key in _plugin_activations:
            result["activation"] = _plugin_activations[key]
            break
    return result


def register(server) -> None:
    """Publish listener state and helpers against the same globals as the tools handlers."""
    bind_module(globals(), server)