"""Family polling regressions through the canonical dispatcher."""
import asyncio
from contextlib import suppress
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.test_hermes_family import LAUNCH_DDL, launch


@pytest.fixture
def reader(tmp_path, monkeypatch):
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("root", source="test")
        db._conn.executescript(LAUNCH_DDL)
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home), instance_id="review",
                                    db=db, epoch=begin_runtime_epoch(db, instance_id="review"))
        connection = AuthorityConnection(authority, object(), {"user_id": "owner", "profile_scope": "host"})
        yield db, authority, connection


async def family(connection, sid="root"):
    return await connection.dispatch({"id": 1, "method": "session.family",
        "params": {"profile": "default", "session_id": sid, "since": 0}})


@pytest.mark.asyncio
async def test_own_store_failure_is_retryable(reader, monkeypatch):
    db, authority, connection = reader
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("fixture unavailable")
    monkeypatch.setattr("gateway.session_family.read_profile", broken)
    try:
        result = await family(connection)
        assert result["error"]["message"] == "storage_unavailable", result
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_profile_enumeration_and_store_signals_are_off_loop(reader, monkeypatch):
    import gateway.session_family as module
    _, _, connection = reader
    loop_thread = threading.get_ident()
    original = module._connections
    def checked(*args):
        assert threading.get_ident() != loop_thread, "blocking profile enumeration ran on loop"
        return original(*args)
    def full_profiles(*args, **kwargs):
        raise AssertionError("Family built full ProfileInfo objects")
    monkeypatch.setattr(module, "_connections", checked)
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", full_profiles)
    try:
        result = await family(connection)
        assert "result" in result, result
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_root_poll_does_not_load_unrelated_history(reader, monkeypatch):
    import gateway.session_family as module
    db, _, connection = reader
    for i in range(1000):
        db.create_session(f"unrelated-{i}", source="test")
        launch(db._conn, f"unrelated-{i}", launcher=f"other-{i}")
    db.create_session("child", source="subagent", parent_session_id="root")
    launch(db._conn, "child")
    db._conn.commit()
    original, loaded = module.read_profile, []
    def counted(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        loaded.append(len(snapshot.sessions) + len(snapshot.launches))
        return snapshot
    monkeypatch.setattr(module, "read_profile", counted)
    try:
        result = await family(connection)
        assert "result" in result, result
        assert [n["session_id"] for n in result["result"]["nodes"]] == ["child"]
        assert sum(loaded) <= 6, loaded
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_live_compressed_root_uses_logical_and_tip_signals(reader):
    from gateway.session_authority import LiveSession
    db, authority, connection = reader
    db._conn.execute("UPDATE sessions SET ended_at=1,end_reason='compression' WHERE id='root'")
    db.create_session("tip", source="test", parent_session_id="root")
    db._conn.commit()
    task = asyncio.create_task(asyncio.Event().wait())
    authority.sessions["root"] = LiveSession(None, "route", task=task)
    try:
        result = await family(connection, "tip")
        assert "result" in result, result
        assert result["result"]["root"]["session_id"] == "tip"
        assert result["result"]["root"]["status"] == "working", result
    finally:
        await connection.close()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
