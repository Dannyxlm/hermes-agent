"""Read-only evidence projections. No heartbeat or prose supplies missing authority."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ProjectionUnavailable(RuntimeError):
    pass


def owner_principal(identity):
    from hermes_cli.config import load_config_readonly
    from hermes_cli.web_server_profiles import _config_profile_scope
    from tui_gateway.methods_browser_control import _is_authenticated_identity
    if not _is_authenticated_identity(identity):
        raise PermissionError("Configured owner required")
    with _config_profile_scope("default"):
        config = load_config_readonly().get("cloudseed_mobile", {})
    if not isinstance(config, dict) or (identity["provider"], identity["user_id"]) != (
            config.get("owner_provider"), config.get("owner_user_id")):
        raise PermissionError("Configured owner required")
    return _digest([identity["provider"], identity["user_id"]])


def profile_homes(names):
    from hermes_cli.profiles import get_profile_dir, profile_exists, validate_profile_name
    if len(set(names)) != len(names):
        raise ValueError("duplicate profiles")
    homes = []
    for name in names:
        validate_profile_name(name)
        home = Path(get_profile_dir(name))
        if not profile_exists(name) or not home.is_dir():
            raise FileNotFoundError("Requested profile does not exist")
        homes.append((name, home.resolve()))
    return homes


@contextmanager
def _read_store(path):
    from hermes_state_holders import read_only_db_uri
    conn = sqlite3.connect(read_only_db_uri(path), uri=True, timeout=0.2)
    deadline = time.monotonic() + 2
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _epoch(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            date = datetime.fromisoformat(str(value))
            if date.tzinfo is None:
                return None
            number = date.timestamp()
        except (ValueError, OverflowError):
            return None
    return number if math.isfinite(number) and number >= 0 else None


def _ordinary_chat(row):
    from tui_gateway.mobile_session_scope import UNLISTED_SOURCES
    if row is None or row["archived"] or (row["source"] or "").strip().lower() in UNLISTED_SOURCES | {"unknown"}:
        return False
    try:
        config = json.loads(row["model_config"] or "{}")
    except (ValueError, TypeError):
        return False
    return isinstance(config, dict) and not config.get("_delegate_from") and row["title"] != "Bot Chat"


def _canonical_lineage(conn, stored_id):
    return conn.execute("""WITH RECURSIVE ancestors(id,parent_session_id,title) AS (
        SELECT id,parent_session_id,title FROM sessions WHERE id=?
        UNION SELECT s.id,s.parent_session_id,s.title FROM sessions s
        JOIN ancestors a ON s.id=a.parent_session_id)
        SELECT 1 FROM ancestors WHERE title='Bot Chat' LIMIT 1""", (stored_id,)).fetchone() is not None


def _session_activity(home, profile, runtimes):
    current = None
    last = None
    path = home / "state.db"
    if not path.is_file():
        if runtimes:
            current = {"kind": "session", "title": None, "work_label": None,
                       "started_at": _epoch(runtimes[0].get("turn_started_at")), "open_chat": None}
        return last, current
    from tui_gateway.mobile_session_scope import UNLISTED_SOURCES
    from hermes_state_common import _sql_json_extract
    hidden = tuple(sorted(UNLISTED_SOURCES | {"unknown"}))
    with _read_store(path) as conn:
        row = conn.execute(f"""WITH RECURSIVE bots(id) AS (
            SELECT id FROM sessions WHERE title='Bot Chat'
            UNION SELECT s.id FROM sessions s JOIN bots b ON s.parent_session_id=b.id)
            SELECT MAX(COALESCE(last_activity_at,started_at)) AS stamp FROM sessions
            WHERE archived=0 AND lower(trim(source)) NOT IN ({','.join('?' for _ in hidden)})
            AND COALESCE({_sql_json_extract('model_config', '$._delegate_from')}, '') = ''
            AND id NOT IN (SELECT id FROM bots)""", hidden).fetchone()
        last = _epoch(row["stamp"])
        for runtime in runtimes:
            row = conn.execute("SELECT id,title,source,archived,model_config FROM sessions WHERE id=?",
                               (runtime.get("session_key"),)).fetchone()
            ordinary = _ordinary_chat(row) and not _canonical_lineage(conn, row["id"])
            title = (row["title"] or "").strip() if row is not None else ""
            current = {"kind": "session", "title": title[:160] or None, "work_label": None,
                       "started_at": _epoch(runtime.get("turn_started_at")),
                       "open_chat": {"profile": profile, "stored_session_id": row["id"]} if ordinary else None}
            if ordinary:
                break
    return last, current


def _live_execution(row):
    from gateway.status import _pid_exists, get_process_start_time, start_time_fingerprints_match
    pid, fingerprint = row.get("pid"), row.get("process_started_at")
    if not isinstance(pid, int) or pid <= 0 or fingerprint is None or not _pid_exists(pid):
        return False
    actual = get_process_start_time(pid)
    return actual is not None and start_time_fingerprints_match(fingerprint, actual)


def _execution_rows(home, start=None, end=None):
    path = home / "cron" / "executions.db"
    if not path.is_file():
        return []
    with _read_store(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
        optional = [name if name in columns else f"NULL AS {name}" for name in ("scheduled_instant", "delivery_outcome")]
        stamp = "COALESCE(julianday(scheduled_instant),julianday(claimed_at))" if "scheduled_instant" in columns else "julianday(claimed_at)"
        where, args = ("status IN ('claimed','running')", ()) if start is None else (
            f"{stamp} >= julianday(?,'unixepoch') AND {stamp} < julianday(?,'unixepoch')", (start, end))
        rows = conn.execute(f"""SELECT id,job_id,status,pid,process_started_at,claimed_at,started_at,finished_at,
            {','.join(optional)} FROM executions WHERE {where} LIMIT 10001""", args).fetchall()
        if len(rows) > 10000:
            raise ProjectionUnavailable("Execution window exceeds bounded read")
        return [dict(row) for row in rows]


def team_activity(homes, runtimes, launch_home):
    agents = []
    for profile, home in homes:
        live = [r for r in runtimes if r.get("running") is True and
                Path(r.get("profile_home") or launch_home).resolve() == home]
        last, current = _session_activity(home, profile, live)
        if current is None:
            for row in _execution_rows(home):
                if _live_execution(row):
                    current = {"kind": "cron", "title": None, "work_label": None,
                               "started_at": _epoch(row["started_at"]), "open_chat": None}
                    break
        agents.append({"profile": profile, "working": True if current else None,
                       "last_human_chat_at": last, "current": current})
    return {"version": 1, "now": time.time(), "complete": False, "working_count": None, "agents": agents}


def validate_window(params):
    start, end = params["start"], params["end"]
    if start < 0 or not start < end or end - start > 31 * 86400:
        raise ValueError("invalid summary window")
    try:
        ZoneInfo(params["timezone"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("unknown IANA timezone") from exc


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _summary_run(profile, row):
    status = row["status"]
    outcome = {"completed": "succeeded", "failed": "failed"}.get(status, "unknown")
    if status in ("claimed", "running") and _live_execution(row):
        outcome = "running"
    delivery = row["delivery_outcome"]
    return {"profile": profile, "job_id": row["job_id"], "occurrence_id": "execution:" + row["id"],
            "started_at": _epoch(row["started_at"]), "finished_at": _epoch(row["finished_at"]),
            "outcome": outcome, "retry_of": None,
            "delivery_summary": delivery if delivery in ("delivered", "failed", "queued", "suppressed", "suppressed_acked", "not_configured") else None}


def _page_cursor(cursor, scope, total):
    if cursor is None:
        return 0
    try:
        decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        offset = decoded["offset"]
        if (set(decoded) != {"scope", "offset"} or decoded["scope"] != scope or
                type(offset) is not int or not 0 < offset < total):
            raise ValueError("stale or foreign cursor")
        return offset
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("stale or foreign cursor") from exc


def cron_summary(homes, params, principal):
    validate_window(params)
    ordered = []
    for profile, home in homes:
        for row in _execution_rows(home, params["start"], params["end"]):
            when = _epoch(row["scheduled_instant"])
            if when is None:
                when = _epoch(row["claimed_at"])
            if when is not None and params["start"] <= when < params["end"]:
                ordered.append((when, profile, row["id"], _summary_run(profile, row)))
    ordered.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    runs = [entry[3] for entry in ordered]
    scope = _digest([principal, sorted(name for name, _ in homes), params["timezone"], params["start"], params["end"], ordered])
    offset = _page_cursor(params["cursor"], scope, len(runs))
    next_offset = offset + params["limit"]
    cursor = base64.urlsafe_b64encode(json.dumps({"scope": scope, "offset": next_offset}).encode()).decode() if next_offset < len(runs) else None
    return {"version": 1, "timezone": params["timezone"], "start": params["start"], "end": params["end"],
            "complete": False, "counts": {"expected": None, "succeeded": None, "retried": None, "needs_attention": None},
            "runs": runs[offset:next_offset], "next_cursor": cursor}


def board_attention(conn, tasks):
    if not tasks:
        return {}
    task_ids = [task.id for task in tasks]
    activity = {}
    for table, columns in (("task_events", ("created_at",)), ("task_comments", ("created_at",)),
                           ("task_runs", ("started_at", "ended_at"))):
        for column in columns:
            for row in conn.execute(f"SELECT task_id,{column} AS stamp FROM {table} WHERE task_id IN ({','.join('?' for _ in task_ids)})", task_ids):
                stamp = _epoch(row["stamp"])
                if stamp is not None:
                    activity[row["task_id"]] = max(activity.get(row["task_id"], 0), stamp)
    result = {}
    for task in tasks:
        status = task.status
        attention = None
        if status in ("todo", "scheduled", "ready", "done", "archived"):
            attention = False
        if status == "blocked" and task.block_kind == "dependency":
            attention = False
        stamps = [_epoch(v) for v in (task.created_at, task.started_at, task.completed_at, activity.get(task.id))]
        result[task.id] = {"owner_attention": attention, "attention_reason": None, "attention_chat": None,
                           "activity_at": max((v for v in stamps if v is not None), default=None)}
    return result
