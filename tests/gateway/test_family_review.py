"""Family polling regressions through the canonical dispatcher."""
import asyncio
from contextlib import suppress
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.test_hermes_family import LAUNCH_DDL, NOW, launch


@pytest.fixture
def loaded_rows(monkeypatch):
    rows = []
    original = sqlite3.connect
    class Cursor(sqlite3.Cursor):
        def __next__(self):
            row = super().__next__()
            columns = {item[0] for item in self.description}
            if "last_activity_description" in columns or "launch_id" in columns:
                rows.append(row)
            return row
    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            return self.cursor(factory=Cursor).execute(sql, parameters)
    def connect(*args, **kwargs):
        return original(*args, **dict(kwargs, factory=Connection))
    monkeypatch.setattr(sqlite3, "connect", connect)
    return rows


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
    _, _, connection = reader
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
async def test_root_poll_does_not_load_unrelated_history(reader, loaded_rows):
    db, _, connection = reader
    for i in range(1000):
        db.create_session(f"unrelated-{i}", source="test")
        launch(db._conn, f"unrelated-{i}", launcher=f"other-{i}")
    db.create_session("child", source="subagent", parent_session_id="root")
    launch(db._conn, "child")
    db._conn.commit()
    try:
        result = await family(connection)
        assert "result" in result, result
        assert [n["session_id"] for n in result["result"]["nodes"]] == ["child"]
        assert len(loaded_rows) <= 6, len(loaded_rows)
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_live_compressed_root_uses_logical_and_tip_signals(reader, running):
    from gateway.session_authority import LiveSession
    db, authority, connection = reader
    db._conn.execute("UPDATE sessions SET ended_at=1,end_reason='compression' WHERE id='root'")
    db.create_session("tip", source="test", parent_session_id="root")
    db._conn.commit()
    task = asyncio.create_task(asyncio.Event().wait()) if running else None
    authority.sessions["root"] = LiveSession(None, "route", task=task)
    try:
        result = await family(connection, "tip")
        assert "result" in result, result
        assert result["result"]["root"]["session_id"] == "tip"
        assert result["result"]["root"]["status"] == ("working" if running else "unknown"), result
    finally:
        await connection.close()
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_overview_loads_only_live_and_recent_launch_rows(reader, loaded_rows):
    db, _, connection = reader
    for i in range(1000):
        db.create_session(f"past-{i}", source="test")
        launch(db._conn, f"past-{i}", launcher="root", cause="unknown", finished=NOW - 7200)
    db.create_session("recent", source="test")
    launch(db._conn, "recent", kind="lane", cause="error", finished=NOW)
    db._conn.commit()
    try:
        result = await connection.dispatch({"id": 1, "method": "family.overview", "params": {"since": NOW - 3600}})
        assert "result" in result, result
        assert [n["session_id"] for n in result["result"]["failed"]] == ["recent"]
        assert len(loaded_rows) <= 4, len(loaded_rows)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_unreached_sibling_does_not_open_a_metadata_snapshot(reader, tmp_path, monkeypatch):
    import gateway.session_family as module
    _, _, connection = reader
    home = tmp_path / "iris"
    home.mkdir()
    with SessionDB(db_path=home / "state.db") as foreign:
        foreign._conn.executescript(LAUNCH_DDL)
        foreign.create_session("unrelated", source="test")
        launch(foreign._conn, "unrelated", profile="iris", launcher="different-chat")
        foreign._conn.commit()
        sibling = SimpleNamespace(authority=SimpleNamespace(db=foreign, profile_id=str(home), sessions={}), actor=connection.actor)
        monkeypatch.setattr(module, "_connections", lambda _: ({"default": connection, "iris": sibling}, []))
        original, loaded = module.read_profile, []
        def counted(profile, *args, **kwargs):
            loaded.append(profile)
            return original(profile, *args, **kwargs)
        monkeypatch.setattr(module, "read_profile", counted)
        try:
            result = await family(connection)
            assert "result" in result, result
            assert result["result"]["nodes"] == []
            assert loaded == ["default"], loaded
        finally:
            await connection.close()
