"""Indexed, read-only selection of Family launch graphs."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def compression_edge(columns, child="child", parent="parent"):
    if "end_reason" not in columns:
        return "0"
    clause = (f"{parent}.end_reason='compression' AND COALESCE({child}.source,'') NOT IN ('subagent','tool')")
    if "model_config" in columns:
        for marker in ("_branched_from", "_delegate_from", "_reset_from"):
            clause += (f" AND COALESCE(CASE WHEN json_valid({child}.model_config) THEN "
                       f"json_extract({child}.model_config,'$.{marker}') END,'') != {parent}.id")
    return clause


def selection(name, columns, has_launches, seeds, since):
    """Return a recursive selector shared by launch and session reads."""
    if seeds is None:
        return "", (), None
    values = ",".join("(?,?,NULL)" for _ in seeds) or "(NULL,NULL,NULL)"
    params = tuple(value for pair in seeds for value in pair)
    edge = compression_edge(columns)
    sql = f"""WITH RECURSIVE reached(profile,sid,lid) AS (
        VALUES {values}
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
    if has_launches:
        sql += """ UNION
        SELECT w.child_profile,w.child_session_id,w.launch_id FROM reached r
        CROSS JOIN work_launches w ON w.launcher_profile=r.profile AND w.launcher_session_id=r.sid
        UNION
        SELECT w.child_profile,w.child_session_id,w.launch_id FROM reached r
        CROSS JOIN work_launches w ON w.parent_launch_id=r.lid WHERE r.profile=?
        """
        params += (name,)
    return sql + ") ", params, "id IN (SELECT sid FROM reached WHERE profile=?)"


def read_metadata(conn, name, tables, columns, fields, *, seeds=None, since=None, lineage_only=False):
    has_launches = "work_launches" in tables and not lineage_only
    cte, params, where = selection(name, columns, has_launches, seeds, since)
    if seeds is not None:
        launches = [dict(r) for r in conn.execute(cte + "SELECT * FROM work_launches WHERE launch_id IN "
                    "(SELECT lid FROM reached WHERE profile=?)", (*params, name))] if has_launches else []
        session_where, session_params = "s." + where, (*params, name)
    elif since is not None:
        launches = [dict(r) for r in conn.execute("SELECT * FROM work_launches WHERE finished_at IS NULL OR finished_at>=?",
                                                  (since,))] if has_launches else []
        seeds = {(r["child_profile"], r["child_session_id"]) for r in launches if r["child_session_id"]}
        seeds.update((r["launcher_profile"], r["launcher_session_id"]) for r in launches)
        # Overview already selected launches by window. Only their session lineage is needed.
        cte, params, where = selection(name, columns, False, sorted(seeds), since)
        session_where, session_params = "s." + where, (*params, name)
    else:
        launches = [dict(r) for r in conn.execute("SELECT * FROM work_launches")] if has_launches else []
        session_where, session_params = "1", ()
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
    if "messages" in tables and has_launches:
        hints = dict(conn.execute("SELECT w.child_session_id, (SELECT m.content FROM messages m "
            "WHERE m.session_id=w.child_session_id AND m.role='assistant' AND m.content IS NOT NULL AND m.content<>'' "
            "AND (m.tool_calls IS NULL OR m.tool_calls='[]') ORDER BY m.id DESC LIMIT 1) "
            "FROM work_launches w WHERE w.kind='lane' AND w.terminal_cause IN "
            "('success','error','user_stop','parent_stop') AND w.finished_at>=? "
            "AND w.launch_id IN (SELECT value FROM json_each(?))",
            (-1e300 if since is None else since, json.dumps([r['launch_id'] for r in launches]))))
        for row in launches:
            text = hints.get(row["child_session_id"])
            row["report_hint"] = text.splitlines()[0] if isinstance(text, str) and text else None
    return sessions, launches, aliases, owners


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
        refs = {(r["launcher_profile"], r["launcher_session_id"])
                for snapshot in profiles.values() for r in snapshot.launches}
        for profile, snapshot in profiles.items():
            seeds = sorted((p, sid) for p, sid in refs if p == profile and sid not in snapshot.sessions)
            if not seeds:
                continue
            try:
                extra = reader(profile, paths[profile],
                                     seeds=seeds, since=since, lineage_only=True)
                filter_snapshot(extra)
                snapshot.sessions.update(extra.sessions)
                snapshot.aliases.update(extra.aliases)
                snapshot.owners.update(extra.owners)
            except (OSError, sqlite3.Error):
                unavailable.append(profile)
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
                snapshot = profiles[profile]
                snapshot.sessions.update(extra.sessions)
                launches = {r["launch_id"]: r for r in snapshot.launches}
                launches.update((r["launch_id"], r) for r in extra.launches)
                snapshot.launches = list(launches.values())
                snapshot.aliases.update(extra.aliases)
                snapshot.owners.update(extra.owners)
                reached.update((profile, sid) for sid in extra.sessions)
    return profiles, unavailable



def lineage_maps(sessions):
    """Compression continuations share one tip; forks and delegates stay separate."""
    children = {}
    for sid, row in sessions.items():
        parent = sessions.get(row.get("parent_session_id"))
        if not parent or parent.get("end_reason") != "compression" or row.get("source") in {"subagent", "tool"}:
            continue
        config = row.get("model_config")
        config = json.loads(config) if isinstance(config, str) and config else {}
        if any(config.get(k) == parent["id"] for k in ("_branched_from", "_delegate_from", "_reset_from")):
            continue
        children.setdefault(parent["id"], []).append(sid)
    def priority(sid):
        row = sessions[sid]
        return (2 if row.get("end_reason") == "compression" else 1 if row.get("ended_at") is None else 0,
                row.get("last_activity_at") or row.get("started_at") or 0, row.get("started_at") or 0, sid)
    forward = {sid: max(ids, key=priority) for sid, ids in children.items()}
    tips, lineages = {}, {}
    for sid in sessions:
        path, tip = set(), sid
        while tip in forward and tip not in path:
            path.add(tip)
            tip = forward[tip]
        tips[sid] = tip
        lineages.setdefault(tip, set()).add(sid)
    return tips, lineages
