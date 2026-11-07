#!/usr/bin/env python3
"""Content-free watcher probe; reads a snapshot and writes ONLY a private sessions-only copy.

Example (run the identical script with base and head --source-root values):
  python scripts/benchmark_session_observation.py --source-root . \
    --snapshot /private/snapshot/state.db --work-dir /private/probe-head --journal

Reports CPU/wall ms and Linux rchar bytes per reader observation, with a commit
before each pass. No session identifiers, titles or messages are printed or saved.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time


def io_bytes():
    with open("/proc/self/io", encoding="utf-8") as stream:
        return int(dict(line.strip().split(": ") for line in stream)["rchar"])


def percentile(values, fraction):
    return sorted(values)[max(0, int(len(values) * fraction + 0.999) - 1)]


def summarize(samples):
    return {name: {"p50": statistics.median(values), "p90": percentile(values, 0.9),
                   "mean": statistics.mean(values)}
            for name, values in zip(("cpu_ms", "wall_ms", "rchar_bytes"), zip(*samples))}


def private_copy(snapshot, work_dir):
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = work_dir / "state.db"
    if target.exists():
        raise ValueError("work-dir must not contain a prior state.db")
    source = sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    source.execute("BEGIN")
    sql = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()[0]
    writer = sqlite3.connect(target)
    writer.execute(sql)
    count = 0
    reader = source.execute("SELECT * FROM sessions")
    marks = ",".join("?" for _ in reader.description)
    while rows := reader.fetchmany(1000):
        writer.executemany(f"INSERT INTO sessions VALUES ({marks})", rows)
        count += len(rows)
    source.rollback()
    source.close()
    writer.commit()
    writer.execute("PRAGMA journal_mode=WAL")
    return target, writer, count


def run(args):
    sys.path.insert(0, str(args.source_root.resolve()))
    from tui_gateway import server, session_change_cursor
    if args.cache_kib or args.unordered:
        import hermes_state
        original = hermes_state._connect_tracked_db

        class ProbeConnection(sqlite3.Connection):
            def execute(self, sql, *parameters):
                if args.unordered and sql.endswith("FROM sessions ORDER BY id"):
                    sql = sql.removesuffix(" ORDER BY id")
                return super().execute(sql, *parameters)

        def connect(*parameters, **kwargs):
            conn = original(*parameters, **{**kwargs, "factory": ProbeConnection})
            if args.cache_kib:
                conn.execute(f"PRAGMA cache_size=-{args.cache_kib}")
            return conn

        hermes_state._connect_tracked_db = connect
    path, writer, count = private_copy(args.snapshot, args.work_dir)
    if args.journal:
        from hermes_state_session_changes import ensure_session_change_journal
        ensure_session_change_journal(writer.cursor())
        writer.commit()
    sid = writer.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
    if args.journal:
        # Steady-state includes pruning, not merely the first minute of an empty journal.
        writer.executemany("UPDATE sessions SET message_count=COALESCE(message_count,0)+1 WHERE id=?",
                           [(sid,)] * 4096)
        writer.commit()
    key = os.path.realpath(path)
    # Independent reader connections/cursors in dashboard and gateway processes use
    # this same entrypoint. Public refresh is the gateway push worker's exact path.
    probe = (lambda: session_change_cursor.refresh(path)) if args.refresh else (lambda: server._session_db_content_sig(path))
    probe()
    results = {}
    for traffic in ("activity_commit", "message_count_commit", "title_commit"):
        samples, writes = [], []
        window_start = None
        for i in range(args.n + 3):
            if i == 3:
                window_start = time.perf_counter()
            time.sleep(args.interval_s)
            field = {"activity_commit": "last_activity_at", "message_count_commit": "message_count",
                     "title_commit": "title"}[traffic]
            value = f"benchmark-{i}" if field == "title" else i + 1000000
            started = time.process_time_ns()
            writer.execute(f"UPDATE sessions SET {field}=? WHERE id=?", (value, sid))
            writer.commit()
            write_ms = (time.process_time_ns() - started) / 1e6
            before = io_bytes()
            cpu, wall = time.process_time_ns(), time.perf_counter_ns()
            probe()
            elapsed = ((time.process_time_ns() - cpu) / 1e6, (time.perf_counter_ns() - wall) / 1e6,
                       io_bytes() - before)
            if i >= 3:
                samples.append(elapsed)
                writes.append(write_ms)
        elapsed_s = time.perf_counter() - window_start
        results[traffic] = {"n": len(samples), **summarize(samples),
                            "writer_cpu_ms_p50": statistics.median(writes),
                            "effective_commits_per_second": len(samples) / elapsed_s,
                            "reader_cpu_ms_per_second": sum(sample[0] for sample in samples) / elapsed_s,
                            "reader_rchar_bytes_per_second": sum(sample[2] for sample in samples) / elapsed_s}
    server._drop_store(key)
    writer.close()
    return {"sessions": count, "journal": args.journal, "entrypoint": "refresh" if args.refresh else "watcher",
            "requested_commit_interval_s": args.interval_s, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--n", type=int, default=32)
    parser.add_argument("--interval-s", type=float, default=0.25)
    parser.add_argument("--journal", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--cache-kib", type=int, default=0)
    parser.add_argument("--unordered", action="store_true")
    args = parser.parse_args()
    if args.n < 1 or args.n > 200:
        parser.error("n must be between 1 and 200")
    # Importing the RPC server redirects sys.stdout to stderr. Preserve the
    # caller's stream so redirected benchmark JSON really lands in its evidence file.
    output = sys.stdout
    print(json.dumps(run(args), indent=2), file=output)


if __name__ == "__main__":
    main()
