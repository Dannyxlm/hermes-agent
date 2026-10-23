"""Native read-only usage and explicit new-chat profile defaults."""
from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


@method("usage.estimate")
def _(rid, params):
    from agent.api_value_pricing import estimate_api_value
    from agent.usage_pricing import CanonicalUsage
    usage = CanonicalUsage(**{k: params.get(k, 0) for k in
                             ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")})
    return _ok(rid, estimate_api_value(params["model"], usage, provider=params.get("provider", "")))


@method("usage.rollup")
@_profile_scoped
def _(rid, params):
    import sqlite3
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from hermes_state_holders import read_only_db_uri
    from hermes_state_rollup import read_usage_rollup
    try:
        zone = ZoneInfo(params.get("timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return _err(rid, 4002, "unknown IANA timezone")
    now = datetime.now(zone)
    days = {"today": 1, "7d": 7, "30d": 30}.get(params.get("window", "7d"))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=(days or 1) - 1)
    path = _active_config_path().parent / "state.db"
    try:
        conn = sqlite3.connect(read_only_db_uri(path), uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            common = {"end": now.timestamp(), "profile": params.get("profile") or _current_profile_name(),
                      "session_id": params.get("session_id"), "limit": params.get("limit", 20),
                      "timezone_name": params.get("timezone", "UTC")}
            if params.get("window") == "all":
                from hermes_state_alltime import read_alltime_usage
                result = read_alltime_usage(conn, **common)
            else:
                result = read_usage_rollup(conn, start=start.timestamp(), **common)
        return _ok(rid, result)
    except sqlite3.Error:
        return _err(rid, 5036, "Timestamped usage storage unavailable; no transcript-scan fallback")
    finally:
        if "conn" in locals():
            conn.close()


@method("config.defaults.get")
@_profile_scoped
def _(rid, params):
    from tui_gateway.profile_defaults import read_defaults
    try:
        return _ok(rid, read_defaults(_active_config_path()))
    except (OSError, ValueError):
        return _err(rid, 5001, "Could not read profile defaults")


@method("config.defaults.set")
@_profile_scoped
def _(rid, params):
    from tui_gateway.methods_browser_control import _is_authenticated_identity, _principal_digest
    from tui_gateway.profile_defaults import validate_default, write_default
    identity = getattr(current_transport(), "auth_identity", None)
    if not _is_authenticated_identity(identity):
        return _err(rid, 4403, "Authenticated human transport required for profile defaults")
    if not params.get("confirm", False):
        return _err(rid, 4002, "Confirm this profile-wide default change for new chats")
    try:
        validate_default(params["key"], params["value"], params.get("provider", ""))
        return _ok(rid, write_default(_active_config_path(), key=params["key"], value=params["value"],
                                      provider=params.get("provider", ""), principal=_principal_digest(identity)))
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    except OSError:
        # Intent receipt survives if the write or applied receipt was interrupted.
        # The client reads defaults again; it must not blindly replay after an error.
        return _err(rid, 5001, "Could not persist/audit profile default; read back before retrying")


def register(server):
    from pydantic import ValidationError
    from tui_gateway.contracts.registry import METHODS

    bind_module(globals(), server, skip=("_",))
    # Core admission rejects unknown keys only. These new methods also validate
    # types/required values BEFORE profile resolution or any write side effect.
    def validated(handler, model, *, explicit_target=False):
        def call(rid, params):
            try:
                values = model.model_validate(params).model_dump()
                if "profile" in values and values["profile"] is not None and not values["profile"].strip():
                    return server._err(rid, 4002, "explicit non-empty profile required")
            except ValidationError:
                return server._err(rid, 4002, "invalid or missing method parameters")
            if explicit_target:
                from hermes_cli.profiles import profile_exists
                # Mutation IDs must be real profiles, not session-info home aliases.
                # Do this before the handler's compatibility-aware profile scope.
                if not profile_exists(values["profile"]):
                    return server._err(rid, 4064, "Requested profile does not exist")
            return handler(rid, values)
        return call
    for name in _registry.names():
        server._methods[name] = validated(server._methods[name], METHODS[name].params,
                                          explicit_target=name == "config.defaults.set")
