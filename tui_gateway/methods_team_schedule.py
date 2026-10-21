"""Owner-admitted Team and schedule RPCs, dispatched by the gateway read pool."""
import sqlite3


def _call(server, name, rid, params):
    from tui_gateway import team_schedule_projection as projection
    from tui_gateway.contracts.registry import METHODS
    try:
        principal = projection.owner_principal(getattr(server.current_transport(), "auth_identity", None))
        parsed = METHODS[name].params.model_validate(params).model_dump()
        homes = projection.profile_homes(parsed["profiles"])
        if name == "team.activity":
            with server._sessions_lock:
                runtimes = [{**{key: row.get(key) for key in ("profile_home", "session_key", "running")},
                             "turn_started_at": server._turn_started_at(row)} for row in server._sessions.values()]
            result = projection.team_activity(homes, runtimes, server._hermes_home)
        else:
            result = projection.cron_summary(homes, parsed, principal)
        return server._ok(rid, result)
    except PermissionError:
        return server._err(rid, 4403, "Configured owner required")
    except FileNotFoundError:
        return server._err(rid, 4064, "Requested profile does not exist")
    except ValueError:
        return server._err(rid, 4002, "Invalid Team or schedule parameters; reload after a changed cursor")
    except (sqlite3.Error, OSError, projection.ProjectionUnavailable):
        return server._err(rid, 5037, "Team or schedule storage unavailable")


def register(server):
    for name in ("team.activity", "cron.summary"):
        server.register_method(name, lambda rid, params, method=name: _call(server, method, rid, params))
