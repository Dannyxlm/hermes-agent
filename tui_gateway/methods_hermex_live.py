"""Hermex live RPC adapters. Reads and Undo run in the gateway's worker pool."""
from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


def _hermex_principal():
    from hermex_live_reads import digest
    peer = current_transport()
    identity = getattr(peer, "auth_identity", None)
    if isinstance(identity, dict) and identity.get("user_id") and identity.get("provider"):
        return digest([identity["provider"], identity["user_id"]])
    # Local stdio inherits process-owner authority. Other local peers stay connection-bound.
    return digest(["local", id(peer)])


def _hermex_rpc(name, operation):
    def handler(rid, params):
        import sqlite3
        from tui_gateway.server import ProfileUnavailableError
        from hermes_state import SessionDB
        from hermex_live_reads import LiveConflict
        from pydantic import ValidationError
        from tui_gateway.contracts import METHODS
        try:
            METHODS[name].params.model_validate(params)
        except ValidationError as exc:
            return _err(rid, 4000, str(exc))
        try:
            profile = params["profile"]
            home = Path(_profile_home(profile) or _hermes_home).resolve()
            if _response_profile_name(profile) != profile or not (home / "state.db").is_file():
                raise ValueError("profile unavailable")
            with _session_profile_runtime_scope({"profile_home": str(home)}):
                with SessionDB(db_path=home / "state.db", read_only=True) as db:
                    result = operation(db, home, params, _hermex_principal())
            return _ok(rid, result)
        except LiveConflict as exc:
            return _err(rid, 4091, str(exc))
        except ProfileUnavailableError:
            return _err(rid, 4064, "Hermex profile unavailable")
        except (ValueError, FileNotFoundError, RuntimeError):
            return _err(rid, 4400, "Hermex target unavailable in this profile")
        except (OSError, sqlite3.Error):
            return _err(rid, 4401, "Hermex state unavailable; read back before retrying a decision")
    return method(name)(handler)


def _hermex_search(db, home, params, principal):
    from hermex_live_reads import search
    return search(db, home, params, principal)


def _hermex_window(db, home, params, principal):
    from hermex_live_reads import window
    return window(db, home, params, principal)


def _hermex_changes(db, home, params, principal):
    from hermex_skill_changes import list_changes
    return list_changes(db, params, principal)


def _hermex_change(db, home, params, principal):
    from hermex_skill_changes import get_change
    return get_change(db, params)


def _hermex_decide(db, home, params, principal):
    from hermex_skill_changes import decide_change
    return decide_change(db, params, principal)


_hermex_rpc("session.search", _hermex_search)
_hermex_rpc("session.messages.window", _hermex_window)
_hermex_rpc("skills.changes.list", _hermex_changes)
_hermex_rpc("skills.changes.get", _hermex_change)
_hermex_rpc("skills.changes.decide", _hermex_decide)


def register(server):
    bind_module(globals(), server)
