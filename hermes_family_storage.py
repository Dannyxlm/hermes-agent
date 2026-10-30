"""Indexed, read-only selection of Family launch graphs."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def compression_edge(columns, child="child", parent="parent", *, lease=False):
    if "end_reason" not in columns:
        return "0"
    # The lease writer allows subagent continuations; Family display aliases do not.
    sources = "('tool')" if lease else "('subagent','tool')"
    clause = (f"{parent}.end_reason='compression' AND COALESCE({child}.source,'') NOT IN {sources}")
    if "model_config" in columns:
        for marker in ("_branched_from", "_delegate_from", "_reset_from"):
            clause += (f" AND COALESCE(CASE WHEN json_valid({child}.model_config) THEN "
                       f"json_extract({child}.model_config,'$.{marker}') END,'') != {parent}.id")
    return clause


def selection(name, columns, has_launches, seeds, since, walk):
    """Return a recursive selector shared by launch and session reads."""
    if seeds is None and since is None:
        return "", (), None
    if seeds is None and has_launches:
        initial = ("SELECT child_profile,child_session_id,launch_id FROM work_launches "
                   "WHERE finished_at IS NULL OR finished_at>=? OR terminal_cause='owner_restart'")
        params = (since,)
    else:
        initial = "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),NULL FROM json_each(?)"
        params = (json.dumps(seeds or []),)
    edge = compression_edge(columns)
    sql = f"""WITH RECURSIVE reached(profile,sid,lid) AS (
        {initial}
        UNION
        SELECT ?,parent.id,NULL FROM reached r CROSS JOIN sessions child ON child.id=r.sid
        JOIN sessions parent ON parent.id=child.parent_session_id
        WHERE r.profile=? AND {edge}
        UNION
        SELECT ?,child.id,NULL FROM reached r CROSS JOIN sessions parent ON parent.id=r.sid
        JOIN sessions child ON child.parent_session_id=parent.id
        WHERE r.profile=? AND {edge}
    """
    params += (name, name, name, name)
    if has_launches and walk == "ancestors":
        sql += """ UNION
        SELECT w.launcher_profile,w.launcher_session_id,NULL FROM reached r
        CROSS JOIN work_launches w ON w.launch_id=r.lid WHERE r.profile=?
        UNION
        SELECT w.child_profile,w.child_session_id,w.launch_id FROM reached r
        CROSS JOIN work_launches w ON w.child_session_id=r.sid WHERE r.profile=?
        UNION
        SELECT w.child_profile,w.child_session_id,w.launch_id FROM reached r
        CROSS JOIN work_launches child ON child.launch_id=r.lid
        JOIN work_launches w ON w.launch_id=child.parent_launch_id WHERE r.profile=?
        """
        params += (name, name, name)
    elif has_launches:
        sql += """ UNION
        SELECT w.child_profile,w.child_session_id,w.launch_id FROM reached r
        CROSS JOIN work_launches w ON w.launcher_profile=r.profile AND w.launcher_session_id=r.sid
        UNION
        SELECT w.child_profile,w.child_session_id,w.launch_id FROM reached r
        CROSS JOIN work_launches w ON w.parent_launch_id=r.lid WHERE r.profile=?
        """
        params += (name,)
    return sql + ") ", params, "id IN (SELECT sid FROM reached WHERE profile=?)"


def read_metadata(conn, name, tables, columns, fields, *, seeds=None, since=None, walk="descendants"):
    has_launches = "work_launches" in tables and walk != "lineage"
    walk = "ancestors" if seeds is None and since is not None else walk
    cte, params, where = selection(name, columns, has_launches, seeds, since, walk)
    launch_where = "launch_id IN (SELECT lid FROM reached WHERE profile=?)" if cte else "1"
    selected_params = (*params, name) if cte else ()
    hints = (",(SELECT m.content FROM messages m WHERE m.session_id=w.child_session_id "
             "AND m.role='assistant' AND m.content IS NOT NULL AND m.content<>'' "
             "AND (m.tool_calls IS NULL OR m.tool_calls='[]') AND w.kind='lane' "
             "AND w.terminal_cause IN ('success','error','user_stop','parent_stop') "
             "AND w.finished_at>=? ORDER BY m.id DESC LIMIT 1) AS report_hint"
             if "messages" in tables and has_launches else "")
    launch_params = ((-1e300 if since is None else since,) if hints else ())
    launches = [dict(r) for r in conn.execute(cte + "SELECT w.*" + hints + " FROM work_launches w WHERE " + launch_where,
                                             (*params, *launch_params, name) if cte else launch_params)] if has_launches else []
    for row in launches:
        text = row.get("report_hint")
        row["report_hint"] = text.splitlines()[0] if isinstance(text, str) and text else None
    session_where, session_params = ("s." + where, selected_params) if cte else ("1", ())
    turn = (",(SELECT MAX(status IN ('started','unknown')) FROM session_admissions WHERE target_session_id=s.id) AS turn_unresolved"
            ",(SELECT MAX(status='terminal') FROM session_admissions WHERE target_session_id=s.id) AS turn_terminal"
            if "session_admissions" in tables else ",NULL AS turn_unresolved,NULL AS turn_terminal")
    sessions = {r["id"]: dict(r) for r in conn.execute(
        cte + f"SELECT {fields}{turn} FROM sessions s WHERE {session_where}", session_params)}
    aliases, owners = {}, {}
    if "state_meta" in tables and sessions:
        prefix = "gateway.local_policy.v1:"
        for row in conn.execute("SELECT substr(key,?), json_extract(value,'$.entry.session_id'), "
                                "json_extract(value,'$.principal_id') FROM state_meta "
                                "WHERE key GLOB ? AND json_valid(value) AND "
                                "json_extract(value,'$.entry.session_id') IN (SELECT value FROM json_each(?))",
                                (len(prefix) + 1, prefix + "*", json.dumps(list(sessions)))):
            aliases[row[0]] = row[1]
            owners[row[0]] = owners[row[1]] = row[2]
    if "session_turn_leases" in tables and sessions:
        read_turn_leases(conn, sessions, columns)
    return sessions, launches, aliases, owners


def read_turn_leases(conn, sessions, columns):
    """One bounded lease read, using the writer's compression-root (not display) walk."""
    from hermes_state import _compression_lock_holder_process_is_dead
    edge = compression_edge(columns, lease=True)
    leased = set()
    rows = conn.execute(f"""WITH RECURSIVE lineage(sid,id) AS (
        SELECT value,value FROM json_each(?)
        UNION
        SELECT r.sid,parent.id FROM lineage r JOIN sessions child ON child.id=r.id
        JOIN sessions parent ON parent.id=child.parent_session_id WHERE {edge}
    ) SELECT r.sid,l.holder FROM lineage r
      JOIN session_turn_leases l ON l.conversation_id=r.id
      WHERE l.expires_at>? AND NOT EXISTS (
        SELECT 1 FROM sessions child JOIN sessions parent ON parent.id=child.parent_session_id
        WHERE child.id=r.id AND {edge})""", (json.dumps(list(sessions)), time.time()))
    for row in rows:
        if not _compression_lock_holder_process_is_dead(row["holder"]):
            leased.add(row["sid"])
    for sid, row in sessions.items():
        row["turn_leased"] = sid in leased


def launch_probe(conn, seeds):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='work_launches' AND type='table'").fetchone() is None:
        return False
    return conn.execute("SELECT 1 FROM json_each(?) r CROSS JOIN work_launches w "
                        "ON w.launcher_profile=json_extract(r.value,'$[0]') "
                        "AND w.launcher_session_id=json_extract(r.value,'$[1]') LIMIT 1",
                        (json.dumps(list(seeds)),)).fetchone() is not None


def probe_path(path, seeds):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA query_only=1")
        return launch_probe(conn, seeds)


def merge_snapshot(snapshot, extra):
    snapshot.sessions.update(extra.sessions)
    launches = {r["launch_id"]: r for r in snapshot.launches}
    launches.update((r["launch_id"], r) for r in extra.launches)
    snapshot.launches = list(launches.values())
    snapshot.aliases.update(extra.aliases)
    snapshot.owners.update(extra.owners)


def overview_ancestors(paths, profiles, reader, since, filter_snapshot):
    seen = {(p, sid) for p, snapshot in profiles.items() for sid in snapshot.sessions}
    unavailable = []
    while True:
        refs = {(r["launcher_profile"], r["launcher_session_id"])
                for snapshot in profiles.values() for r in snapshot.launches} - seen
        refs = {(p, sid) for p, sid in refs if p in profiles}
        if not refs:
            return unavailable
        seen.update(refs)
        for profile, snapshot in profiles.items():
            seeds = sorted((p, sid) for p, sid in refs if p == profile)
            if not seeds:
                continue
            try:
                extra = reader(profile, paths[profile], seeds=seeds, since=since, walk="ancestors")
            except (OSError, sqlite3.Error):
                unavailable.append(profile)
                continue
            filter_snapshot(extra)
            merge_snapshot(snapshot, extra)
            seen.update((profile, sid) for sid in extra.sessions)


def read_graph(paths, reader, *, root=None, since=None, filter_snapshot=lambda snapshot: None, probe=None):
    from hermes_family import Profile
    probe = probe or (lambda profile, seeds: probe_path(paths[profile], seeds))
    profiles, unavailable = {}, []
    for profile, path in paths.items():
        try:
            if root and profile != root[0] and not probe(profile, [root]):
                profiles[profile] = Profile(profile, {}, [])
                continue
            snapshot = reader(profile, path, seeds=[root] if root else None, since=since)
        except (OSError, sqlite3.Error):
            unavailable.append(profile)
            continue
        filter_snapshot(snapshot)
        profiles[profile] = snapshot
    if not root:
        unavailable.extend(overview_ancestors(paths, profiles, reader, since, filter_snapshot))
    if root:
        reached = {root}
        for snapshot in profiles.values():
            reached.update((snapshot.name, sid) for sid in snapshot.sessions)
        queried = {p: {root} for p in paths}
        while True:
            pending = {p: reached - queried[p] for p in profiles if reached - queried[p]}
            if not pending:
                break
            for profile, seeds in pending.items():
                queried[profile].update(seeds)
                # A profile's local recursive closure already covers its own new ids.
                seeds = {seed for seed in seeds if seed[0] != profile}
                if not seeds:
                    continue
                try:
                    if not probe(profile, seeds):
                        continue
                    extra = reader(profile, paths[profile], seeds=sorted(seeds), since=since)
                except (OSError, sqlite3.Error):
                    unavailable.append(profile)
                    continue
                filter_snapshot(extra)
                merge_snapshot(profiles[profile], extra)
                reached.update((profile, sid) for sid in extra.sessions)
    return profiles, unavailable



def resolve_paths(edges):
    resolved = {}
    for start in edges:
        path, current = set(), start
        while current in edges and current not in resolved and current not in path:
            path.add(current)
            current = edges[current]
        target = resolved.get(current, current)
        resolved.update((key, target) for key in path)
        resolved.setdefault(current, target)
    return resolved


def compression_parent(row, sessions):
    parent = sessions.get(row.get("parent_session_id"))
    if not parent or parent.get("end_reason") != "compression" or row.get("source") in {"subagent", "tool"}:
        return None
    config = row.get("model_config")
    try:
        config = json.loads(config) if isinstance(config, str) and config else config
    except json.JSONDecodeError:
        config = None
    if isinstance(config, dict) and any(config.get(k) == parent["id"] for k in ("_branched_from", "_delegate_from", "_reset_from")):
        return None
    return parent["id"]


def compression_owners(sessions):
    """The logical claim owner stays at the root while compression advances the display tip."""
    return resolve_paths({sid: parent for sid, row in sessions.items()
                          if (parent := compression_parent(row, sessions))})


def compression_tips(sessions):
    """Normalize every compression alias to its preferred continuation, including stale siblings."""
    parents, children = {}, {}
    for sid, row in sessions.items():
        parent = compression_parent(row, sessions)
        if parent:
            parents[sid] = parent
            children.setdefault(parent, []).append(sid)
    def priority(sid):
        row = sessions[sid]
        return (2 if row.get("end_reason") == "compression" else 1 if row.get("ended_at") is None else 0,
                row.get("last_activity_at") or row.get("started_at") or 0, row.get("started_at") or 0, sid)
    roots = resolve_paths(parents)
    tips = resolve_paths({sid: max(ids, key=priority) for sid, ids in children.items()})
    return {sid: tips.get(roots.get(sid, sid), roots.get(sid, sid)) for sid in sessions}
