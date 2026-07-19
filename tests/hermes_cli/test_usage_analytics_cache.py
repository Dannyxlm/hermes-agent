"""Usage analytics stale-while-revalidate and persisted-cache contracts."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading

from hermes_cli import web_server


def _payload(days: int = 7):
    return {
        "daily": [],
        "by_model": [],
        "by_task": [],
        "totals": {"total_sessions": 1},
        "period_days": days,
        "skills": {"summary": {}, "top_skills": []},
        "tools": [],
    }


def test_persisted_cache_round_trip_is_private_and_db_bound(
    tmp_path: Path, monkeypatch
):
    hermes_home = tmp_path / "hermes-home"
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"db-one")
    monkeypatch.setattr(web_server, "get_hermes_home", lambda: hermes_home)

    payload = _payload()
    web_server._write_persisted_usage_analytics(str(db_path), 7, payload)

    cache_path = web_server._usage_analytics_persisted_path(str(db_path), 7)
    assert web_server._read_persisted_usage_analytics(str(db_path), 7) == payload
    if os.name != "nt":
        assert cache_path.stat().st_mode & 0o777 == 0o600
        assert cache_path.parent.stat().st_mode & 0o777 == 0o700

    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"db-two")
    os.replace(replacement, db_path)
    assert web_server._read_persisted_usage_analytics(str(db_path), 7) is None


def test_stale_payload_returns_while_single_flight_refresh_runs(
    tmp_path: Path, monkeypatch
):
    payload = _payload()
    refresh_started = threading.Event()
    refresh_release = threading.Event()

    monkeypatch.setattr(
        web_server,
        "_usage_analytics_db_path",
        lambda _profile: tmp_path / "state.db",
    )
    monkeypatch.setattr(
        web_server,
        "_read_persisted_usage_analytics",
        lambda *_args: payload,
    )

    def _refresh(_db_path, _days, _db_identity, _bucket):
        refresh_started.set()
        assert refresh_release.wait(5)
        return _payload()

    monkeypatch.setattr(web_server, "_compute_usage_analytics_cached", _refresh)

    result = asyncio.run(web_server.get_usage_analytics(days=7))
    assert result == payload
    assert refresh_started.wait(2)
    refresh_release.set()


def test_pending_limit_uses_stale_payload_instead_of_blocking(
    tmp_path: Path, monkeypatch
):
    payload = _payload()
    monkeypatch.setattr(
        web_server,
        "_usage_analytics_db_path",
        lambda _profile: tmp_path / "state.db",
    )
    monkeypatch.setattr(
        web_server,
        "_read_persisted_usage_analytics",
        lambda *_args: payload,
    )

    def _busy(*_args):
        raise web_server.HTTPException(status_code=503, detail="busy")

    monkeypatch.setattr(web_server, "_submit_usage_analytics", _busy)

    assert asyncio.run(web_server.get_usage_analytics(days=7)) == payload


def test_atomic_db_replacement_gets_distinct_cache_and_single_flight_key(
    tmp_path: Path, monkeypatch
):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"first database inode")
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []

    def _refresh(*args):
        calls.append(args)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(5)
        return _payload()

    monkeypatch.setattr(web_server, "_compute_usage_analytics_cached", _refresh)

    first = web_server._submit_usage_analytics(str(db_path), 7, 123)
    assert first_started.wait(2)

    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"second database inode")
    os.replace(replacement, db_path)
    second = web_server._submit_usage_analytics(str(db_path), 7, 123)

    try:
        assert second is not first
        release_first.set()
        assert first.result(timeout=5) == _payload()
        assert second.result(timeout=5) == _payload()
        assert len(calls) == 2
        assert calls[0][2] != calls[1][2]
    finally:
        release_first.set()
