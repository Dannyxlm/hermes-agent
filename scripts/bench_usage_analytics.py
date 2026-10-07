"""Content-free benchmark: /api/analytics/usage tool/skill tallies, uncached vs cached, on a real state.db.

Usage: PYTHONPATH=<worktree> <release python> scripts/bench_usage_analytics.py [--store PATH] [--no-old] [days ...]
Read-only: opens the profile's state.db the way the dashboard route does. Prints timings, counts and
equality flags only (no tool names, skill names or message content). --store points the sidecar at a
scratch path (run twice to measure a dashboard restart).
"""
import argparse
import os
import time

from agent.insights import InsightsEngine
from agent.insights_usage_cache import SessionUsageCache
from hermes_cli.web_server_sessions import _open_session_db_for_profile, _session_db_path_for_profile


def norm(b):
    return sorted((t["tool"], t["count"]) for t in b["tools"]), b["skills"]


ap = argparse.ArgumentParser()
ap.add_argument("--store")
ap.add_argument("--no-old", action="store_true")
ap.add_argument("days", nargs="*", type=int, default=[30])
args = ap.parse_args()

st = os.stat(_session_db_path_for_profile(None))
db = _open_session_db_for_profile(None, read_only=True)
conn = db._conn
cache = SessionUsageCache(store_path=args.store, db_identity=(st.st_dev, st.st_ino))
eng = InsightsEngine(db)
for days in args.days:
    conn.execute("BEGIN")  # one WAL snapshot so both paths read identical rows
    try:
        old = t_old = None
        if not args.no_old:
            t = time.perf_counter(); old = eng.get_usage_breakdown(days=days); t_old = time.perf_counter() - t
        t = time.perf_counter(); first = eng.get_usage_breakdown(days=days, cache=cache); t_first = time.perf_counter() - t
        n_first = cache.last_refreshed
        t = time.perf_counter(); again = eng.get_usage_breakdown(days=days, cache=cache); t_again = time.perf_counter() - t
    finally:
        conn.execute("COMMIT")
    line = (f"days={days} cached_first={t_first:.2f}s (re-read {n_first} sessions) "
            f"cached_again={t_again * 1000:.0f}ms (re-read {cache.last_refreshed}) tools={len(first['tools'])} "
            f"skills={first['skills']['summary']['distinct_skills_used']}")
    if old is not None:
        line += f" | uncached={t_old:.2f}s equal={norm(old) == norm(first) == norm(again)}"
    print(line, flush=True)
db.close()
