"""Regression tests for Track 1 RPC performance caches (profiles, mobile.bots, family, model.options)."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

import hermes_family
from hermes_cli import profiles as profiles_mod
from tui_gateway import profile_roster_cache as roster_cache
from tui_gateway import server


@pytest.fixture(autouse=True)
def _clean_caches():
    roster_cache.invalidate()
    hermes_family.invalidate_profile_cache()
    yield
    roster_cache.invalidate()
    hermes_family.invalidate_profile_cache()


def test_list_profiles_skips_liveness_and_aliases_when_disabled(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "alpha").mkdir(parents=True)
    (home / "profiles" / "alpha" / "config.yaml").write_text("model:\n  default: test\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles_mod, "_check_gateway_running", lambda *a, **kw: pytest.fail("liveness should be skipped"))
    monkeypatch.setattr(profiles_mod, "_served_by_running_multiplexer", lambda *a, **kw: pytest.fail("multiplexer check should be skipped"))
    monkeypatch.setattr(profiles_mod, "build_alias_map", lambda *a, **kw: pytest.fail("alias map should be skipped"))

    result = profiles_mod.list_profiles(lazy_skill_count=True, include_liveness=False, include_aliases=False)
    names = [p.name for p in result]
    assert "default" in names and "alpha" in names


def _make_wal_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, source TEXT, archived INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO sessions (id, title, source) VALUES (?, ?, ?)", ("s1", "Bot Chat", "desktop"))
    conn.commit()
    settled = time.time_ns() - 60 * 1_000_000_000
    os.utime(db_path, ns=(settled, settled))
    return conn


def test_store_signature_uses_wal_shm_header_and_invalidates_same_ms_commit(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _make_wal_db(db_path)
    try:
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("Chat AAAA", "s1"))
        conn.commit()

        calls = []
        v1 = roster_cache.cached_mobile_bot_canonical(tmp_path, lambda: (calls.append(1), {"v": 1})[1])
        v2 = roster_cache.cached_mobile_bot_canonical(tmp_path, lambda: (calls.append(2), {"v": 2})[1])
        assert v1 == {"v": 1} and v2 == {"v": 1}
        assert len(calls) == 1

        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("Chat BBBB", "s1"))
        conn.commit()
        v3 = roster_cache.cached_mobile_bot_canonical(tmp_path, lambda: (calls.append(3), {"v": 3})[1])
        assert v3 == {"v": 3}
        assert len(calls) == 2
    finally:
        conn.close()


def test_family_read_profile_cache_hits_isolates_and_invalidates(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _make_wal_db(db_path)
    try:
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("Title 1", "s1"))
        conn.commit()

        p1 = hermes_family.read_profile("default", db_path)
        assert p1.sessions["s1"]["title"] == "Title 1"
        p1.sessions["s1"]["title"] = "MUTATED"
        p1.sessions["injected"] = {"id": "injected"}

        p2 = hermes_family.read_profile("default", db_path)
        assert p2.sessions["s1"]["title"] == "Title 1"
        assert "injected" not in p2.sessions

        traced = []
        p_traced = hermes_family.read_profile("default", db_path, trace=traced.append)
        assert len(traced) > 0 and p_traced.sessions["s1"]["title"] == "Title 1"

        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("Title 2", "s1"))
        conn.commit()
        p3 = hermes_family.read_profile("default", db_path)
        assert p3.sessions["s1"]["title"] == "Title 2"
    finally:
        conn.close()


def test_model_options_invalidates_on_config_yaml_change_and_isolates_copies(tmp_path, monkeypatch):
    from types import SimpleNamespace
    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    ctx = SimpleNamespace(current_provider="openai", current_model="gpt-5")
    builds = []
    monkeypatch.setattr(server, "_profile_home", lambda name: home if name == "work" else None)
    monkeypatch.setattr(server, "_model_picker_context", lambda agent: ctx)
    monkeypatch.setattr(server, "_MODEL_OPTIONS_CACHE", {}, raising=False)

    def build(_ctx, **kwargs):
        builds.append(kwargs)
        return {"providers": [{"slug": f"b-{len(builds)}"}]}

    monkeypatch.setattr("hermes_cli.inventory.build_model_options_payload", build)

    r1 = server._methods["model.options"](1, {"profile": "work"})["result"]
    assert r1["providers"] == [{"slug": "b-1"}]
    r1["providers"].clear()

    r2 = server._methods["model.options"](2, {"profile": "work"})["result"]
    assert r2["providers"] == [{"slug": "b-1"}]
    assert len(builds) == 1

    (home / "config.yaml").write_text("model:\n  default: gpt-6\n", encoding="utf-8")
    r3 = server._methods["model.options"](3, {"profile": "work"})["result"]
    assert r3["providers"] == [{"slug": "b-2"}]
    assert len(builds) == 2
