"""Incremental per-session tool/skill tallies (agent/insights_usage_cache.py).

The cached path must return exactly what the uncached InsightsEngine queries
return, re-read only sessions whose message rows changed, and survive a
dashboard restart through its sidecar without trusting a stale or foreign one.
"""

import json
import os
import time

import pytest

from agent.insights import InsightsEngine
from agent.insights_usage_cache import _ASSISTANT_CALLS, _TOOL_NAMES, SessionUsageCache
from hermes_state import SessionDB

DAY = 86400


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _session(db, sid, *, source="cli", days_ago=1.0):
    db.create_session(session_id=sid, source=source, model="m")
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (time.time() - days_ago * DAY, sid))


def _call(db, sid, *names, skill_args=None):
    calls = [{"function": {"name": n, "arguments": json.dumps(skill_args or {})}} for n in names]
    db.append_message(sid, role="assistant", content="x", tool_calls=calls)


def _seed(db):
    _session(db, "gw", source="telegram", days_ago=2)
    db.append_message("gw", role="tool", content="r", tool_name="search_files")
    db.append_message("gw", role="tool", content="r", tool_name="terminal")
    _session(db, "cli", days_ago=3)
    _call(db, "cli", "search_files")
    _call(db, "cli", "skill_view", skill_args={"name": "github-pr-workflow"})
    _session(db, "both", days_ago=5)
    _call(db, "both", "search_files", "terminal")
    db.append_message("both", role="tool", content="r", tool_name="search_files")
    db.append_message("both", role="tool", content="r", tool_name="terminal")
    _call(db, "both", "skill_manage", skill_args={"name": "github-code-review"})
    _call(db, "both", "skill_view", skill_args={"name": "github-pr-workflow"})
    _session(db, "malformed", days_ago=6)
    db._conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?, 'assistant', 'x', ?, ?)",
        ("malformed", json.dumps([{"function": {"name": "read_file"}}, {"function": "oops"}, "junk"]), time.time()),
    )
    _call(db, "malformed", "skill_view")  # no skill name -> ignored by both paths
    _session(db, "old", days_ago=45)
    _call(db, "old", "patch")
    db._conn.commit()


def _norm(breakdown):
    return sorted((t["tool"], t["count"]) for t in breakdown["tools"]), breakdown["skills"]


@pytest.mark.parametrize("days,source", [(30, None), (90, None), (7, None), (30, "cli"), (30, "telegram")])
def test_cached_breakdown_matches_uncached_engine(db, days, source):
    _seed(db)
    engine = InsightsEngine(db)
    cache = SessionUsageCache()
    expected = engine.get_usage_breakdown(days=days, source=source)
    assert _norm(engine.get_usage_breakdown(days=days, source=source, cache=cache)) == _norm(expected)
    assert _norm(engine.get_usage_breakdown(days=days, source=source, cache=cache)) == _norm(expected)
    assert cache.last_refreshed == 0


def test_per_session_max_not_global_max(db):
    """#9814: a call recorded both ways in one session counts once; disjoint sessions sum."""
    _seed(db)
    tools = dict((t["tool"], t["count"]) for t in
                 InsightsEngine(db).get_usage_breakdown(days=30, cache=SessionUsageCache())["tools"])
    assert tools["search_files"] == 3  # gw(1) + cli(1) + both(max(1, 1))
    assert tools["terminal"] == 2
    assert tools["read_file"] == 1 and "patch" not in tools


def test_only_changed_sessions_are_reread(db):
    _seed(db)
    engine, cache = InsightsEngine(db), SessionUsageCache()
    engine.get_usage_breakdown(days=30, cache=cache)
    assert cache.last_refreshed == 4  # gw, cli, both, malformed (old is outside the window)

    engine.get_usage_breakdown(days=30, cache=cache)
    assert cache.last_refreshed == 0

    _call(db, "cli", "terminal")
    db._conn.commit()
    tools = dict((t["tool"], t["count"]) for t in engine.get_usage_breakdown(days=30, cache=cache)["tools"])
    assert cache.last_refreshed == 1
    assert tools["terminal"] == 3

    # A delete changes the fingerprint too.
    db._conn.execute("DELETE FROM messages WHERE session_id = 'gw' AND tool_name = 'terminal'")
    db._conn.commit()
    after = engine.get_usage_breakdown(days=30, cache=cache)
    assert cache.last_refreshed == 1
    assert _norm(after) == _norm(engine.get_usage_breakdown(days=30))


def test_wider_window_reuses_narrower_entries(db):
    _seed(db)
    engine, cache = InsightsEngine(db), SessionUsageCache()
    engine.get_usage_breakdown(days=30, cache=cache)
    engine.get_usage_breakdown(days=90, cache=cache)
    assert cache.last_refreshed == 1  # only "old" is new to the cache
    engine.get_usage_breakdown(days=7, cache=cache)
    assert cache.last_refreshed == 0


def test_cache_cap_falls_back_to_a_full_reread(db):
    _seed(db)
    engine, cache = InsightsEngine(db), SessionUsageCache(max_entries=3)
    first = engine.get_usage_breakdown(days=30, cache=cache)
    assert _norm(first) == _norm(engine.get_usage_breakdown(days=30))


def test_tally_queries_probe_by_session_not_full_scan(db):
    _seed(db)
    marks = ",".join("?" * 3)
    for sql in (_TOOL_NAMES, _ASSISTANT_CALLS):
        plan = " | ".join(r["detail"] for r in db._conn.execute(
            "EXPLAIN QUERY PLAN " + sql.format(marks=marks), ("gw", "cli", "both")))
        assert "session_id=?" in plan, plan  # an index probe per session, never a table scan


class TestSidecar:
    def _cache(self, tmp_path, db_path, identity=None):
        st = os.stat(db_path)
        return SessionUsageCache(store_path=str(tmp_path / "cache" / "usage-tallies.json"),
                                 db_identity=identity or (st.st_dev, st.st_ino))

    def test_restart_loads_sidecar_and_rereads_nothing(self, db, tmp_path):
        _seed(db)
        engine = InsightsEngine(db)
        first = self._cache(tmp_path, db.db_path)
        expected = engine.get_usage_breakdown(days=30, cache=first)
        sidecar = tmp_path / "cache" / "usage-tallies.json"
        assert sidecar.exists()
        assert oct(sidecar.stat().st_mode & 0o777) == "0o600"
        assert "github-pr-workflow" in sidecar.read_text()  # names and counts only
        assert '"x"' not in sidecar.read_text()  # never message content

        restarted = self._cache(tmp_path, db.db_path)
        assert _norm(engine.get_usage_breakdown(days=30, cache=restarted)) == _norm(expected)
        assert restarted.last_refreshed == 0

        # Entries loaded from disk are still revalidated by fingerprint.
        _call(db, "cli", "terminal")
        db._conn.commit()
        revalidated = self._cache(tmp_path, db.db_path)
        tools = dict((t["tool"], t["count"]) for t in engine.get_usage_breakdown(days=30, cache=revalidated)["tools"])
        assert revalidated.last_refreshed == 1
        assert tools["terminal"] == 3

    def test_foreign_or_corrupt_sidecar_is_ignored(self, db, tmp_path):
        _seed(db)
        engine = InsightsEngine(db)
        expected = engine.get_usage_breakdown(days=30)
        engine.get_usage_breakdown(days=30, cache=self._cache(tmp_path, db.db_path))  # writes the sidecar

        foreign = self._cache(tmp_path, db.db_path, identity=(1, 2))
        assert _norm(engine.get_usage_breakdown(days=30, cache=foreign)) == _norm(expected)
        assert foreign.last_refreshed == 4

        (tmp_path / "cache" / "usage-tallies.json").write_text("{not json")
        corrupt = self._cache(tmp_path, db.db_path)
        assert _norm(engine.get_usage_breakdown(days=30, cache=corrupt)) == _norm(expected)
        assert corrupt.last_refreshed == 4

    def test_unwritable_store_never_fails_the_request(self, db, tmp_path):
        _seed(db)
        blocker = tmp_path / "blocked"
        blocker.write_text("a file where the cache dir should be")
        cache = SessionUsageCache(store_path=str(blocker / "usage-tallies.json"), db_identity=(0, 0))
        engine = InsightsEngine(db)
        assert _norm(engine.get_usage_breakdown(days=30, cache=cache)) == _norm(engine.get_usage_breakdown(days=30))
