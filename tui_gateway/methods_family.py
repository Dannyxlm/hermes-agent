"""Legacy native Family metadata reads. No agent construction or profile-store bootstrap."""
from pathlib import Path

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


def _family_profiles(*, root=None, since=None):
    from hermes_family import Profile, read_profile
    from hermes_family_storage import read_graph
    from hermes_cli.profiles import get_profile_dir, list_profile_names
    from tui_gateway.ws_legacy_fallback import connection_profile_home
    own = connection_profile_home()
    paths, empty, unavailable = {}, {}, []
    for profile in list_profile_names():
        home = get_profile_dir(profile)
        if own and home.resolve() != Path(own).resolve():
            unavailable.append(profile)
        elif not (home / "state.db").exists():
            empty[profile] = Profile(profile, {}, [])
        else:
            paths[profile] = home / "state.db"
    profiles, failed = read_graph(paths, read_profile, root=root, since=since)
    profiles.update(empty)
    return profiles, unavailable + failed


def _family_live(profiles, unavailable):
    import sys
    from hermes_cli.profiles import get_profile_dir
    homes = {p: get_profile_dir(p).resolve() for p in profiles}
    from hermes_family_runtime import resident_signals
    from tui_gateway import server_requests
    with _sessions_lock:
        sessions = tuple(_sessions.items())
    stoppable_helpers, process_keys = set(), set()
    registry = sys.modules.get("tools.delegate_tool_registry")
    for runtime_id, session in sessions:
        try:
            transport, owner = _current_session_steer_authority(runtime_id)
        except ProfileUnavailableError:
            home = Path(session.get("profile_home") or _hermes_home).resolve()
            unavailable.extend(p for p, path in homes.items() if path == home)
            continue
        if owner is not session:
            continue
        home = str(Path(session.get("profile_home") or _hermes_home).resolve())
        process_keys.add((home, session.get("session_key")))
        if registry is not None:
            with registry._active_subagents_lock:
                stoppable_helpers.update(id(r.get("agent")) for r in registry._active_subagents.values()
                    if r.get("owner_session_id") == runtime_id and r.get("owner_session_record") is session
                    and registry._subagent_transport_matches(r, transport))
    signals = resident_signals(homes,
        can_stop_helper=lambda agent: id(agent) in stoppable_helpers,
        can_stop_process=lambda process: (str(Path(process.owner_profile_home).resolve()), process.session_key) in process_keys)
    for runtime_id, session in sessions:
        if session.get("_finalized"):
            continue
        home = Path(session.get("profile_home") or _hermes_home).resolve()
        profile = next((p for p in profiles if homes[p] == home), None)
        if profile is None:
            continue
        sid = session.get("session_key") or getattr(session.get("agent"), "session_id", None)
        if not sid:
            continue
        signal = signals.setdefault((profile, sid), {})
        pending = next((r["method"] for r in server_requests.open_requests(runtime_id) if r["method"] in {"approval", "clarify"}), None)
        try:
            _, owner = _current_session_steer_authority(runtime_id)
        except ProfileUnavailableError:
            unavailable.append(profile)
            continue
        running = True if signal.get("running") is True else session.get("agent_running")
        signal.update(running=running, pending_kind=pending, can_answer=owner is session)
    return signals


def _family_root_visible(profile, stored_id):
    """Readable under family.overview's rule: a transport, a profile this connection may see, and
    the chat stored there. The chat need not be hosted: the Agents page opens families of chats
    that aren't open on the phone. Hosting still governs Stop and Answer (see _family_live)."""
    from hermes_cli.profiles import get_profile_dir
    from hermes_family import read_profile
    from tui_gateway.ws_legacy_fallback import connection_profile_home
    if current_transport() is None:
        return False
    home = get_profile_dir(profile).resolve()
    own = connection_profile_home()
    if own and home != Path(own).resolve():
        return False
    if not (home / "state.db").is_file():
        return False
    snapshot = read_profile(profile, home / "state.db", seeds=[(profile, stored_id)], walk="lineage")
    return stored_id in snapshot.sessions


@method("session.family")
def _family_read_rpc(rid, params):
    from hermes_family import Family
    from hermes_cli.profiles import profile_exists
    from pydantic import ValidationError
    from tui_gateway.contracts.family import FamilySessionParams
    try:
        params = FamilySessionParams.model_validate(params, strict=True).model_dump()
    except ValidationError:
        return _err(rid, 4000, "invalid family parameters")
    profile, sid = params["profile"], params["session_id"]
    if not profile_exists(profile):
        return _err(rid, 4001, "session not found or not owned by this transport")
    try:
        if not _family_root_visible(profile, sid):
            return _err(rid, 4001, "session not found or not owned by this transport")
        import time
        since = params.get("since")
        since = time.time() - 43200 if since is None else since
        profiles, unavailable = _family_profiles(root=(profile, sid), since=since)
        signals = _family_live(profiles, unavailable)
        family = Family({p: s for p, s in profiles.items() if p not in unavailable},
                        live=signals, unavailable_profiles=unavailable)
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
    import time
    since = params.get("since")
    since = time.time() - 43200 if since is None else since
    profiles, unavailable = _family_profiles(since=since)
    signals = _family_live(profiles, unavailable)
    family = Family({p: s for p, s in profiles.items() if p not in unavailable},
                    live=signals, unavailable_profiles=unavailable)
    return _ok(rid, family.overview(since=params.get("since"), coming_up_within=params.get("coming_up_within")))


def register(server):
    bind_module(globals(), server)
