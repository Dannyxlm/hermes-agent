"""Read-only launch graph and pure Family v1 projections.

Edges are recorded provenance. Activity text and a missing end timestamp never prove liveness.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import time


LIVE = frozenset({"working", "quiet", "needs_you"})
FINISHED = frozenset({"done", "failed", "stopped"})
STEPS = frozenset({"starting", "thinking", "writing", "tool", "waiting_helpers", "waiting_input",
                   "compressing", "retrying", "finalizing", "idle"})
TERMINAL = {"success": "done", "error": "failed", "failed_start": "failed",
            "user_stop": "stopped", "parent_stop": "stopped", "owner_restart": "paused_by_restart"}
SESSION_FIELDS = ("id", "source", "user_id", "chat_id", "parent_session_id", "title", "started_at", "ended_at", "model",
                  "last_activity_at", "last_activity_description", "last_activity_kind", "last_activity_tool", "runtime_generation")


@dataclass
class Profile:
    name: str
    sessions: dict[str, dict]
    launches: list[dict]
    aliases: dict[str, str] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)


def read_profile(name: str, path: Path, *, trace=None) -> Profile:
    """One metadata cut per profile, constant statements, no schema bootstrap or writer handle."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        if trace is not None:
            conn.set_trace_callback(trace)
        conn.execute("PRAGMA query_only=1")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        fields = ",".join(f's."{field}"' if field in columns else f'NULL AS "{field}"' for field in SESSION_FIELDS)
        inflight = ("LEFT JOIN (SELECT target_session_id, MAX(status IN ('started','unknown')) AS unresolved, "
                    "MAX(status='terminal') AS terminal FROM session_admissions GROUP BY target_session_id) a "
                    "ON a.target_session_id=s.id") if "session_admissions" in tables else ""
        turn = ",a.unresolved AS turn_unresolved,a.terminal AS turn_terminal" if inflight else ",NULL AS turn_unresolved,NULL AS turn_terminal"
        sessions = {r["id"]: dict(r) for r in conn.execute(f"SELECT {fields}{turn} FROM sessions s {inflight}")}
        launches = [dict(r) for r in conn.execute("SELECT * FROM work_launches")] if "work_launches" in tables else []
        aliases, owners = {}, {}
        if "state_meta" in tables:
            prefix = "gateway.local_policy.v1:"
            for row in conn.execute("SELECT substr(key,?), json_extract(value,'$.entry.session_id'), "
                                    "json_extract(value,'$.principal_id') FROM state_meta "
                                    "WHERE key GLOB ? AND json_valid(value)", (len(prefix) + 1, prefix + "*")):
                if row[1]:
                    aliases[row[0]] = row[1]
                    owners[row[0]] = owners[row[1]] = row[2]
        if "messages" in tables and "work_launches" in tables:
            hints = dict(conn.execute("SELECT m.session_id, m.content FROM messages m JOIN "
                "(SELECT MAX(m.id) AS id FROM messages m JOIN work_launches w ON w.child_session_id=m.session_id "
                "WHERE w.kind='lane' AND w.terminal_cause IN ('success','error','user_stop','parent_stop') "
                "AND m.role='assistant' AND m.content IS NOT NULL AND m.content<>'' "
                "AND (m.tool_calls IS NULL OR m.tool_calls='[]') GROUP BY m.session_id) last ON last.id=m.id"))
            for launch in launches:
                text = hints.get(launch["child_session_id"])
                launch["report_hint"] = text.splitlines()[0] if isinstance(text, str) and text else None
        conn.rollback()
    return Profile(name, sessions, launches, aliases, owners)


def terminal_cause_for(row: dict, live: dict):
    cause = row.get("terminal_cause")
    if cause not in TERMINAL or cause == "owner_restart":
        cause = live.get("terminal_cause") or cause
    return cause


def activity_at(row: dict, live: dict):
    return max((value for value in (row.get("last_activity_at"), live.get("last_activity_at"))
                if value is not None), default=None)


def status_for(row: dict, live: dict, now: float) -> str:
    cause = terminal_cause_for(row, live)
    if cause in TERMINAL:
        return TERMINAL[cause]
    if live.get("pending_kind") in {"approval", "clarify"}:
        return "needs_you"
    if live.get("running") is True:
        threshold = 1200 if row["kind"] in {"lane", "teammate"} else 600
        activity = activity_at(row, live)
        return "quiet" if activity is not None and now - activity >= threshold else "working"
    return "unknown"


def node_id(profile: str, key: str, *, launch=False) -> str:
    return json.dumps([profile, "launch" if launch else "session", key], separators=(",", ":"), ensure_ascii=False)


class Family:
    def __init__(self, profiles: dict[str, Profile], *, live=None, now=None, unavailable_profiles=()):
        self.profiles = profiles
        self.live = live or {}
        self.now = time.time() if now is None else now
        referenced = {row[key] for snapshot in profiles.values() for row in snapshot.launches
                      for key in ("launcher_profile", "child_profile") if row[key] not in profiles}
        self.unavailable_profiles = sorted(set(unavailable_profiles) | referenced)
        self.rows = {}
        self.by_session = {}
        self.by_launch = {}
        self.children = defaultdict(list)
        self._index()

    def _index(self):
        for profile, snapshot in self.profiles.items():
            for launch in snapshot.launches:
                # The child's store is authoritative. A deleted bound session stays deleted.
                sid = launch["child_session_id"]
                if launch["child_profile"] != profile or (sid and sid not in snapshot.sessions):
                    continue
                key = node_id(profile, sid or launch["launch_id"], launch=not sid)
                row = {**snapshot.sessions.get(sid, {}), **launch, "profile": profile, "session_id": sid}
                self.rows[key] = row
                self.by_launch[(profile, launch["launch_id"])] = key
                if sid:
                    self.by_session[(profile, sid)] = key
        for profile, snapshot in self.profiles.items():
            for sid, session in snapshot.sessions.items():
                key = self.by_session.setdefault((profile, sid), node_id(profile, sid))
                if key not in self.rows:
                    self.rows[key] = {**session, "profile": profile, "session_id": sid, "kind": "chat"}
        for key, row in self.rows.items():
            profile = row["profile"]
            parent = None
            if row.get("launch_id"):
                if row.get("parent_launch_id"):
                    parent = self.by_launch.get((profile, row["parent_launch_id"]))
                else:
                    parent = self.by_session.get((row["launcher_profile"], row["launcher_session_id"]))
            elif row.get("source") == "subagent" and row.get("parent_session_id"):
                parent = self.by_session.get((profile, row["parent_session_id"]))
                row["kind"] = "helper"
            row["parent"] = parent
            if parent and parent != key:
                self.children[parent].append(key)

    def _node(self, key, *, root=False):
        row = self.rows[key]
        profile, sid = row["profile"], row["session_id"]
        live = self.live.get((profile, sid or row.get("backend_ref") or row.get("launch_id")), {})
        status = status_for(row, live, self.now)
        pending = live.get("pending_kind") if status == "needs_you" else None
        step = row.get("last_activity_kind") or live.get("step_kind")
        step = step if status in LIVE and step in STEPS else None
        session = self.profiles[profile].sessions.get(sid, {})
        kind = "chat" if root else row["kind"]
        actions = ["open"] if sid or row.get("launch_id") else []
        needs = {"kind": pending, "session_id": sid, "profile": profile} if pending and sid else None
        if needs and live.get("can_answer"):
            actions.append("answer")
        if status == "paused_by_restart" and kind in {"lane", "helper", "teammate"}:
            actions.append("resume")
        if status in LIVE and kind in {"helper", "process"} and live.get("can_stop"):
            actions.append("stop")
        return {"node_id": key, "parent_node_id": None if root else row.get("parent"), "kind": kind,
                "profile": profile, "session_id": sid, "title": row.get("title") if row.get("launch_id") else None,
                "full_title": session.get("title"), "batch_id": row.get("batch_id"), "status": status,
                "step_kind": step, "step_tool": (row.get("last_activity_tool") or live.get("step_tool")) if step == "tool" else None,
                "activity_text": row.get("last_activity_description"),
                "started_at": row.get("created_at", row.get("started_at")),
                "ended_at": next((value for value in (row.get("finished_at"), row.get("ended_at"), live.get("ended_at"))
                                  if value is not None), None),
                "last_activity_at": activity_at(row, live),
                "model": row.get("model"), "terminal_cause": terminal_cause_for(row, live),
                "needs_you": needs, "next_run_at": row.get("next_run_at") if kind == "check_in" else None,
                "exit_code": live.get("exit_code") if kind == "process" else None,
                "command": live.get("command") if kind == "process" else None,
                "report_hint": (row.get("report_hint") or live.get("report_hint")) if kind == "lane" and status in FINISHED else None,
                "child_count": len(self.children[key]), "actions": actions}

    def _walk(self, root):
        queue, seen, out = deque([(root, 0)]), {root}, []
        truncated = False
        while queue:
            parent, depth = queue.popleft()
            if depth >= 64:
                truncated |= bool(self.children[parent])
                continue
            for key in self.children[parent]:
                if key in seen:
                    continue
                seen.add(key)
                out.append(self._node(key))
                queue.append((key, depth + 1))
        return out, truncated

    def session_family(self, profile, session_id, *, since=None, limit=200):
        root = self.by_session[(profile, session_id)]
        since = self.now - 43200 if since is None else since
        limit = 200 if limit is None else limit
        nodes, truncated = self._walk(root)
        selected = {n["node_id"] for n in nodes if n["status"] not in FINISHED
                    or n["ended_at"] is not None and n["ended_at"] >= since}
        nodes = sorted((n for n in nodes if n["node_id"] in selected), key=lambda n: (n["started_at"] or 0, n["node_id"]))
        removable = sorted((n for n in nodes if n["status"] in FINISHED),
                           key=lambda n: (n["ended_at"] or 0, n["started_at"] or 0, n["node_id"]))
        drop = {n["node_id"] for n in removable[:max(0, len(nodes) - limit)]}
        truncated |= len(nodes) > limit
        nodes = [n for n in nodes if n["node_id"] not in drop]
        batches = {}
        for node in nodes:
            if batch := node["batch_id"]:
                row = self.rows[node["node_id"]]
                key = (node["profile"], batch, node["parent_node_id"])
                entry = batches.setdefault(key, {"batch_id": batch, "batch_name": row.get("batch_name"),
                    "parent_node_id": node["parent_node_id"], "created_at": node["started_at"], "size": 0})
                entry["size"] += 1
                entry["created_at"] = min(entry["created_at"], node["started_at"])
        return {"protocol_version": 1, "root": self._node(root, root=True), "nodes": nodes,
                "batches": list(batches.values()), "complete": not self.unavailable_profiles,
                "unavailable_profiles": self.unavailable_profiles, "truncated": truncated, "generated_at": self.now}

    def _helper_parent_idle(self, row):
        profile = row.get("launcher_profile", row["profile"])
        sid = row.get("launcher_session_id", row.get("parent_session_id"))
        signal = self.live.get((profile, sid), {})
        if signal.get("running") is True or signal.get("pending_kind") in {"approval", "clarify"}:
            return False
        if signal.get("running") is False:
            return True
        parent = self.by_session.get((profile, sid))
        if parent and status_for(self.rows[parent], signal, self.now) in FINISHED | {"paused_by_restart"}:
            return True
        snapshot = self.profiles.get(profile)
        session = snapshot.sessions.get(sid, {}) if snapshot else {}
        return session.get("turn_terminal") == 1 and session.get("turn_unresolved") == 0

    def overview(self, *, since=None, coming_up_within=None):
        since = self.now - 43200 if since is None else since
        within = 7200 if coming_up_within is None else coming_up_within
        roots, earlier = [], []
        all_nodes = {key: self._node(key) for key, row in self.rows.items()
                     if row.get("parent") or (row.get("launch_id") and row["launcher_profile"] not in self.profiles)}
        needs = {key: node for key, node in all_nodes.items() if node["status"] == "needs_you"}
        coming = {key: node for key, node in all_nodes.items() if node["kind"] == "check_in"
                  and node["next_run_at"] is not None and self.now <= node["next_run_at"] <= self.now + within}
        for key, row in self.rows.items():
            if row.get("parent") or row["kind"] != "chat" or not self.children[key]:
                continue
            result = self.session_family(row["profile"], row["session_id"], since=since, limit=len(self.rows))
            nodes, root = result["nodes"], result["root"]
            all_nodes.update((n["node_id"], n) for n in nodes)
            live = [n for n in nodes if n["status"] in LIVE]
            failed = sum(n["status"] == "failed" for n in nodes)
            needs.update((n["node_id"], n) for n in nodes if n["status"] == "needs_you")
            coming.update((n["node_id"], n) for n in nodes if n["kind"] == "check_in" and n["next_run_at"] is not None
                          and self.now <= n["next_run_at"] <= self.now + within)
            if live:
                live_parents = {n["parent_node_id"] for n in live}
                roots.append({"root": root, "live_leaves": [n for n in live if n["node_id"] not in live_parents],
                              "live_count": len(live), "total_count": len(nodes) + 1,
                              "needs_you_count": sum(n["status"] == "needs_you" for n in nodes), "failed_count": failed})
            elif nodes and all(n["status"] in FINISHED and n["ended_at"] is not None for n in nodes):
                ended = max(n["ended_at"] for n in nodes)
                if ended >= since:
                    earlier.append({"root": root, "total_count": len(nodes) + 1, "failed_count": failed, "ended_at": ended})
        team = []
        for profile in sorted(self.profiles):
            live = [n for n in all_nodes.values() if n["profile"] == profile and n["status"] in LIVE]
            team.append({"profile": profile, "state": "needs_you" if any(n["status"] == "needs_you" for n in live)
                         else "working" if live else "resting", "live_count": len(live)})
        roots.sort(key=lambda r: max((n["last_activity_at"] or n["started_at"] or 0 for n in r["live_leaves"]), default=0), reverse=True)
        earlier.sort(key=lambda r: r["ended_at"], reverse=True)
        failed = [node for key, node in all_nodes.items() if node["status"] == "failed"
                  and node["ended_at"] is not None and node["ended_at"] >= since
                  and (node["kind"] in {"lane", "teammate", "check_in_run", "process", "background"}
                       or node["kind"] == "helper" and self._helper_parent_idle(self.rows[key]))]
        failed.sort(key=lambda node: (node["ended_at"], node["node_id"]), reverse=True)
        return {"protocol_version": 1, "team": team, "needs_you": list(needs.values()), "roots": roots,
                "coming_up": sorted(coming.values(), key=lambda n: n["next_run_at"]), "earlier": earlier,
                "failed": failed,
                "complete": not self.unavailable_profiles, "unavailable_profiles": self.unavailable_profiles,
                "generated_at": self.now}
