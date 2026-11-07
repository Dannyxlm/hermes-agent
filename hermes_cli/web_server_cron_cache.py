"""Definition reuse for dashboard cron reads. Live state is never memoized."""
from collections import OrderedDict
import json
import logging
from pathlib import Path
from threading import Lock


logger = logging.getLogger(__name__)
_definitions: OrderedDict[tuple, tuple[tuple, bytes]] = OrderedDict()
_lock = Lock()
_ledgers: OrderedDict[tuple, tuple[tuple, bytes]] = OrderedDict()
_MAX_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _stamp(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (None,)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _retain(cache, key, stamp, raw):
    if stamp is not None and all(part is not None for part in stamp) and len(raw) <= _MAX_BYTES:
        with _lock:
            cache[key] = stamp, raw
            cache.move_to_end(key)
            while len(cache) > 32 or sum(len(entry[1]) for entry in cache.values()) > _MAX_TOTAL_BYTES:
                cache.popitem(last=False)


def execution_snapshot(ids):
    from cron import executions
    from hermes_constants import get_hermes_home
    clean = list(dict.fromkeys(str(job_id) for job_id in ids if job_id))
    if not clean:
        return {}, []
    path = executions.EXECUTIONS_FILE or get_hermes_home().resolve() / "cron" / "executions.db"
    key = str(path.resolve()), tuple(clean)
    before = _stamp(path), _stamp(Path(str(path) + "-wal"))
    with _lock:
        saved = _ledgers.get(key)
        raw = saved[1] if saved is not None and saved[0] == before else None
    if raw is None:
        latest = executions.latest_executions(clean)
        marks = ",".join("?" for _ in clean)
        with executions._transaction() as conn:
            rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM executions WHERE job_id IN ({marks}) AND status IN ('claimed','running') "
                "ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC", clean).fetchall()]
        after = _stamp(path), _stamp(Path(str(path) + "-wal"))
        if before == after:
            _retain(_ledgers, key, before, json.dumps((latest, rows)).encode())
    else:
        latest, rows = json.loads(raw)
    return latest, rows


def _execution_observation(ids):
    from cron import executions
    latest, rows = execution_snapshot(ids)
    live = {}
    for row in rows:
        if row["job_id"] not in live and executions._row_owner_live(row):
            live[row["job_id"]] = row
    return latest, live


def list_dashboard_jobs(store, include_disabled):
    """Reuse normalized definitions, refreshing ledger state and liveness separately."""
    path = store._current_cron_store().jobs_file
    key = str(path.resolve()), bool(include_disabled)
    before = _stamp(path)
    with _lock:
        cached = _definitions.get(key)
        raw = cached[1] if before is not None and cached is not None and cached[0] == before else None
        if raw is not None:
            _definitions.move_to_end(key)
    if raw is not None:
        jobs = json.loads(raw)
    else:
        jobs = [store._normalize_job_record(job) for job in store.load_jobs()]
        if not include_disabled:
            jobs = [job for job in jobs if job.get("enabled", True)]
        definitions = [{**job, "latest_execution": None, "live_execution": None} for job in jobs]
        raw = json.dumps(definitions, ensure_ascii=False, separators=(",", ":")).encode()
        if before is not None and before == _stamp(path):
            _retain(_definitions, key, before, raw)
    try:
        latest, live = _execution_observation([job.get("id", "") for job in jobs])
    except Exception:
        logger.debug("Cron execution observation unavailable", exc_info=True)
        latest, live = {}, {}
    for job in jobs:
        job["latest_execution"] = latest.get(job.get("id", ""))
        job["live_execution"] = live.get(job.get("id", ""))
    return jobs
