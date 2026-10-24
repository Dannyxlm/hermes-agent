"""Legacy native Family metadata reads. No agent construction or profile-store bootstrap."""
from pathlib import Path

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


def _family_profiles():
    import sqlite3
    from hermes_family import read_profile
    from hermes_cli.profiles import list_profiles
    from tui_gateway.ws_legacy_fallback import connection_profile_home
    own = connection_profile_home()
    profiles, unavailable = {}, []
    for profile in list_profiles():
        home = Path(profile.path)
        if own and home.resolve() != Path(own).resolve():
            unavailable.append(profile.name)
            continue
        try:
            profiles[profile.name] = read_profile(profile.name, home / "state.db")
        except (OSError, sqlite3.Error):
            unavailable.append(profile.name)
    return profiles, unavailable


def _family_live(profiles):
    import sys
    from hermes_family_runtime import resident_signals
    from tui_gateway import server_requests
    with _sessions_lock:
        sessions = tuple(_sessions.items())
    stoppable_helpers, process_keys = set(), set()
    registry = sys.modules.get("tools.delegate_tool_registry")
    for runtime_id, session in sessions:
        transport, owner = _current_session_steer_authority(runtime_id)
        if owner is not session:
            continue
        home = str(Path(session.get("profile_home") or _hermes_home).resolve())
        process_keys.add((home, session.get("session_key")))
        if registry is not None:
            with registry._active_subagents_lock:
                stoppable_helpers.update(id(r.get("agent")) for r in registry._active_subagents.values()
                    if r.get("owner_session_id") == runtime_id and r.get("owner_session_record") is session
                    and registry._subagent_transport_matches(r, transport))
    signals = resident_signals({p: Path(_profile_home(p) or _hermes_home) for p in profiles},
        can_stop_helper=lambda agent: id(agent) in stoppable_helpers,
        can_stop_process=lambda process: (str(Path(process.owner_profile_home).resolve()), process.session_key) in process_keys)
    for runtime_id, session in sessions:
        if session.get("_finalized"):
            continue
        home = Path(session.get("profile_home") or _hermes_home).resolve()
        profile = next((p for p in profiles if Path(_profile_home(p) or _hermes_home).resolve() == home), None)
        if profile is None:
            continue
        sid = session.get("session_key") or getattr(session.get("agent"), "session_id", None)
        if not sid:
            continue
        signal = signals.setdefault((profile, sid), {})
        pending = next((r["method"] for r in server_requests.open_requests(runtime_id) if r["method"] in {"approval", "clarify"}), None)
        _, owner = _current_session_steer_authority(runtime_id)
        running = True if signal.get("running") is True else session.get("agent_running")
        signal.update(running=running, pending_kind=pending, can_answer=owner is session)
    return signals


def _family_root_owned(profile, stored_id):
    home = Path(_profile_home(profile) or _hermes_home).resolve()
    with _sessions_lock:
        sessions = tuple(_sessions.items())
    for runtime_id, session in sessions:
        if Path(session.get("profile_home") or _hermes_home).resolve() != home:
            continue
        if stored_id not in {session.get("session_key"), getattr(session.get("agent"), "session_id", None)}:
            continue
        _, owner = _current_session_steer_authority(runtime_id)
        if owner is session:
            return True
    return False


@method("session.family")
def _family_read_rpc(rid, params):
    from hermes_family import Family
    from pydantic import ValidationError
    from tui_gateway.contracts.family import FamilySessionParams
    try:
        params = FamilySessionParams.model_validate(params, strict=True).model_dump()
    except ValidationError:
        return _err(rid, 4000, "invalid family parameters")
    profile, sid = params["profile"], params["session_id"]
    try:
        if not _family_root_owned(profile, sid):
            return _err(rid, 4001, "session not found or not owned by this transport")
        profiles, unavailable = _family_profiles()
        family = Family(profiles, live=_family_live(profiles), unavailable_profiles=unavailable)
        return _ok(rid, family.session_family(profile, sid, since=params.get("since"), limit=params.get("limit")))
    except (ProfileUnavailableError, KeyError):
        return _err(rid, 4001, "session not found or not owned by this transport")


@method("family.overview")
def _family_overview_rpc(rid, params):
    from hermes_family import Family
    from pydantic import ValidationError
    from tui_gateway.contracts.family import FamilyOverviewParams
    try:
        params = FamilyOverviewParams.model_validate(params, strict=True).model_dump()
    except ValidationError:
        return _err(rid, 4000, "invalid family parameters")
    if current_transport() is None:
        return _err(rid, 4001, "session not found or not owned by this transport")
    profiles, unavailable = _family_profiles()
    family = Family(profiles, live=_family_live(profiles), unavailable_profiles=unavailable)
    return _ok(rid, family.overview(since=params.get("since"), coming_up_within=params.get("coming_up_within")))


def register(server):
    bind_module(globals(), server)
