"""Static cron JSON fragments with fresh heartbeat and owner observations."""
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from threading import Lock

from fastapi.responses import JSONResponse

from hermes_cli import web_server_cron as io
from hermes_cli.web_server_cron_cache import _stamp, execution_snapshot


@dataclass(frozen=True)
class CronWireSnapshot:
    revision: tuple
    fragments: tuple
    candidates: tuple


logger = logging.getLogger(__name__)
_snapshot: CronWireSnapshot | None = None
_lock = Lock()
_MAX_BYTES = 4 * 1024 * 1024


def _targets(profile):
    requested = (profile or "all").strip()
    names = ([str(item["name"]) for item in io._cron_profile_dicts() if item.get("name")]
             if requested.lower() == "all" else [requested])
    return [io._cron_profile_home(name) for name in names]


def _revision(profile, targets):
    from cron import executions
    stamps = []
    for name, home in targets:
        ledger = executions.EXECUTIONS_FILE or home / "cron/executions.db"
        files = [home / "cron/jobs.json", ledger, Path(str(ledger) + "-wal")]
        values = tuple(_stamp(path) for path in files)
        if any(value is None for value in values):
            return None
        stamps.append((name, str(home), str(home.resolve()), str(ledger), values))
    return (profile or "all").strip().lower(), tuple(stamps)


def _job_fragments(job):
    text = JSONResponse(content=job).body.decode()
    decoder = json.JSONDecoder()
    cursor, start = 1, 0
    fragments = []
    while text[cursor] != "}":
        key, colon = decoder.raw_decode(text, cursor)
        value_start = colon + 1
        _value, end = decoder.raw_decode(text, value_start)
        if key in {"live_execution", "scheduler_heartbeat_age_s"}:
            fragments.extend((text[start:value_start].encode(), (key, job["profile"], job.get("id", ""))))
            start = end
        cursor = end + (text[end] == ",")
    fragments.append(text[start:].encode())
    return fragments


def _build(revision, targets, jobs):
    fragments = [b"["]
    candidates = []
    for index, job in enumerate(jobs):
        if index:
            fragments.append(b",")
        fragments.extend(_job_fragments(job))
    fragments.append(b"]")
    for name, home in targets:
        ids = [job.get("id", "") for job in jobs if job.get("profile") == name]
        with io._cron_store_scope(home):
            _latest, rows = execution_snapshot(ids)
        candidates.append((name, json.dumps(rows).encode()))
    size = sum(len(part) for part in fragments if isinstance(part, bytes)) + sum(len(raw) for _, raw in candidates)
    if size > _MAX_BYTES:
        return None
    return CronWireSnapshot(revision, tuple(fragments), tuple(candidates))


def _render(snapshot, targets):
    from cron import executions
    from cron import jobs as store
    observations = {}
    for name, home in targets:
        with io._cron_store_scope(home):
            age = store.get_ticker_heartbeat_age()
        observations[("scheduler_heartbeat_age_s", name, "")] = JSONResponse(content=age).body
    for name, raw in snapshot.candidates:
        for row in json.loads(raw):
            key = ("live_execution", name, row["job_id"])
            if key not in observations and executions._row_owner_live(row):
                observations[key] = JSONResponse(content=row).body
    return b"".join(part if isinstance(part, bytes) else observations.get(
        (part[0], part[1], "" if part[0] == "scheduler_heartbeat_age_s" else part[2]), b"null")
        for part in snapshot.fragments)


def cron_jobs_body(profile, build):
    global _snapshot
    targets = _targets(profile)
    before = _revision(profile, targets)
    with _lock:
        saved = _snapshot
    if before is not None and saved is not None and saved.revision == before:
        try:
            return _render(saved, targets)
        except Exception:
            logger.debug("Cron observation unavailable; rebuilding through the list fallback", exc_info=True)
    failures = []
    jobs = build(profile, failures=failures)
    if before is not None and not failures:
        try:
            candidate = _build(before, targets, jobs)
        except Exception:
            logger.debug("Cron response reuse unavailable", exc_info=True)
            candidate = None
        if candidate is not None and before == _revision(profile, targets):
            with _lock:
                _snapshot = candidate
    return bytes(JSONResponse(content=jobs).body)
