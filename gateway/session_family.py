"""Native Family reads reuse profile grants without adopting or hydrating sessions."""
import asyncio
from functools import partial
from pathlib import Path
import sqlite3

from gateway.session_contract import SessionRef
from hermes_family import Family, read_profile
from hermes_family_runtime import resident_signals
from hermes_state_runtime import RuntimeStoreError


def handlers(connection):
    return {"session.family": partial(read, connection), "family.overview": partial(read, connection, overview=True)}


def _connections(connection):
    from gateway.session_authorities import local_bots, served_profile_name, require_serving
    require_serving(connection.authority)
    if "session:read" not in connection.actor.capabilities or connection.actor.profile_id != connection.authority.profile_id:
        raise RuntimeStoreError("permission_denied")
    own = served_profile_name(connection.authority.profile_id)
    allowed, unavailable = {own: connection}, []
    from hermes_cli.profiles import list_profile_names
    known = set(list_profile_names())
    for bot in local_bots(connection.authority.runner):
        known.add(bot.name)
        if bot.name == own:
            continue
        try:
            sibling = connection._sibling_for(bot.name)
        except RuntimeStoreError:
            unavailable.append(bot.name)
            continue
        if sibling is None or bot.problem or bot.retiring:
            unavailable.append(bot.name)
        else:
            allowed[bot.name] = sibling
    unavailable.extend(known - set(allowed) - set(unavailable))
    return allowed, unavailable


def _pending(live, generation):
    from tools.approval import list_gateway_approvals
    for prompt_id, (route, prompt) in tuple(live.controls.pending.items()):
        if generation is None or prompt["execution_generation"] != generation:
            continue
        active = prompt_id in live.controls.remote_responders
        if prompt["kind"] == "clarify":
            active |= not route.event.is_set()
        else:
            active |= any(p["request_id"] == prompt_id for p in list_gateway_approvals(route))
        if active:
            return prompt["kind"]
    return None


def _stop_context(connection, target, live, registry):
    if "session:control" not in connection.actor.capabilities:
        return None
    authority = connection.authority
    store = getattr(authority.runner, "session_store", None)
    if store is None:
        return None
    with store._lock:
        entry = store._entries.get(live.route)
        if entry is None or entry.session_id != target:
            return None
    lookup = getattr(authority.runner, "_resident_agent_for", None)
    agent = lookup(live.route) if lookup else None
    if agent is None:
        return None
    owners = {target, agent.session_id}
    if registry is not None:
        with registry._active_subagents_lock:
            owners.update(r["subagent_id"] for r in registry._active_subagents.values()
                          if registry._is_descendant_of(r.get("agent"), agent))
    return agent, (str(Path(authority.profile_id).resolve()), {live.route, target}, owners)


def _live_signals(connections, profiles):
    import sys
    stop_parents, process_owners, session_signals = [], [], {}
    registry = sys.modules.get("tools.delegate_tool_registry")
    for profile, connection in connections.items():
        if profile not in profiles:
            continue
        authority, snapshot = connection.authority, profiles[profile]
        from hermes_family_storage import compression_tips
        tips = compression_tips(snapshot.sessions)
        for sid, live in tuple(authority.sessions.items()):
            if live.superseded:
                continue
            try:
                authority.authorize(connection.actor, SessionRef(authority.profile_id, sid), "session:read")
            except RuntimeStoreError:
                continue
            target = snapshot.aliases.get(sid, sid)
            tip = tips.get(target, target)
            pending = _pending(live, snapshot.sessions.get(tip, {}).get("runtime_generation"))
            active_task = live.task is not None and not live.task.done()
            stored = snapshot.sessions.get(tip, {})
            running = True if active_task else False if stored.get("turn_unresolved") == 0 and not pending else None
            session_signals[(profile, target)] = {"running": running, "pending_kind": pending,
                "can_answer": ("session:respond" if pending == "clarify" else "session:approve") in connection.actor.capabilities}
            session_signals[(profile, tip)] = dict(session_signals[(profile, target)])
            context = _stop_context(connection, target, live, registry)
            if context:
                parent, owners = context
                stop_parents.append(parent)
                process_owners.append(owners)
    def can_stop_helper(agent):
        return registry is not None and any(registry._is_descendant_of(agent, parent) for parent in stop_parents)
    def can_stop_process(process):
        return any(str(Path(process.owner_profile_home).resolve()) == home and process.session_key in keys
                   and (process.owner_task_id or process.task_id) in owners for home, keys, owners in process_owners)
    signals = resident_signals({p: c.authority.profile_id for p, c in connections.items() if p in profiles},
                              can_stop_helper=can_stop_helper, can_stop_process=can_stop_process)
    for key, signal in session_signals.items():
        resident = signals.setdefault(key, {})
        if signal["running"] is None:
            signal.pop("running")
        elif resident.get("running") is True:
            signal["running"] = True
        resident.update(signal)
    return signals


def _filter_principal(snapshot, connection):
    profile = snapshot.name
    # Local sessions retain the same principal ownership rule as authority.authorize.
    if "session:operator" not in connection.actor.capabilities:
        denied = {sid for sid, owner in snapshot.owners.items() if owner != connection.actor.subject}
        denied.update(sid for sid, row in snapshot.sessions.items()
                      if (row.get("source") == "local" or str(row.get("chat_id") or "").startswith("local-"))
                      and row.get("user_id") != connection.actor.subject)
        snapshot.sessions = {sid: row for sid, row in snapshot.sessions.items() if sid not in denied}
        snapshot.launches = [r for r in snapshot.launches if r["child_session_id"] not in denied
                             and not (r["launcher_profile"] == profile and r["launcher_session_id"] in denied)]


def _read_profiles(connections, *, root=None, since=None):
    from hermes_family_storage import launch_probe, read_graph
    paths = {p: Path(c.authority.db.db_path) for p, c in connections.items()}
    def probe(profile, seeds):
        with connections[profile].authority.db._read_ctx() as conn:
            return launch_probe(conn, seeds)
    return read_graph(paths, read_profile, root=root, since=since, probe=probe,
                      filter_snapshot=lambda snapshot: _filter_principal(snapshot, connections[snapshot.name]))


async def read(connection, ref, params, *, overview=False):
    from pydantic import ValidationError
    from tui_gateway.contracts.family import FamilySessionParams, FamilyOverviewParams
    try:
        params = (FamilyOverviewParams if overview else FamilySessionParams).model_validate(params, strict=True).model_dump()
    except ValidationError as exc:
        raise RuntimeStoreError("invalid_params") from exc
    import time
    from gateway.session_authorities import served_profile_name
    profile = served_profile_name(connection.authority.profile_id)
    since = params.get("since")
    since = time.time() - 43200 if since is None else since
    connections, unavailable = await asyncio.to_thread(_connections, connection)
    profiles, failed = await asyncio.to_thread(_read_profiles, connections,
        root=None if overview else (profile, params["session_id"]), since=since)
    unavailable += failed
    if not overview:
        if profile in failed:
            raise RuntimeStoreError("storage_unavailable")
        if profile not in profiles or params["session_id"] not in profiles[profile].sessions:
            raise RuntimeStoreError("not_found")
        # Stored IDs are the wire identity. Live local receipt IDs are not substituted.
        sid = params["session_id"]
        logical = next((key for key, target in profiles[profile].aliases.items() if target == sid), sid)
        if logical in connection.authority.sessions:
            connection.authority.authorize(connection.actor, SessionRef(connection.authority.profile_id, logical), "session:read")
    signals = await asyncio.to_thread(_live_signals, connections, profiles)
    def project():
        family = Family(profiles, live=signals, unavailable_profiles=unavailable)
        if overview:
            return family.overview(since=params.get("since"), coming_up_within=params.get("coming_up_within"))
        return family.session_family(profile, sid, since=params.get("since"), limit=params.get("limit"))
    return await asyncio.to_thread(project)
