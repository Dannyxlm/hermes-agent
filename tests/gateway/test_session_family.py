"""Family reads use the existing RPC dispatchers and do not restore runtimes."""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.test_hermes_family import LAUNCH_DDL, launch, session, NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("field,expected", [("backend_ref", "delegate-target"),
    ("root_session_id", "root"), ("root_profile", "default")])
async def test_canonical_family_entrypoint(tmp_path, monkeypatch, field, expected):
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("root", source="test")
        db._conn.executescript(LAUNCH_DDL)
        session(db._conn, "child", source="subagent", parent="root")
        launch(db._conn, "child", backend="delegate-target", cause="success", finished=NOW - 1)
        db._conn.commit()
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home),
                                     instance_id="family", db=db, epoch=begin_runtime_epoch(db, instance_id="family"))
        authority.sessions["root"] = LiveSession(None, "route")
        connection = AuthorityConnection(authority, object(), {"user_id": "owner"})
        monkeypatch.setattr("gateway.session_local_adoption.adopt_before_restore",
                            _no_restore)
        response = await connection.dispatch({"id": 1, "method": "session.family",
                                               "params": {"profile": "default", "session_id": "root", "since": 0}})
        assert "result" in response, response
        assert response["result"]["nodes"][0]["session_id"] == "child"
        node = response["result"]["nodes"][0]
        assert field in node, node
        assert node[field] == expected
        assert response["result"]["root"].get("root_session_id") == "root"
        assert response["result"]["root"].get("root_profile") == "default"
        assert "backend_ref" in response["result"]["root"]
        assert response["result"]["root"]["backend_ref"] is None
        overview = await connection.dispatch({"id": 2, "method": "family.overview", "params": {"since": 0}})
        assert "result" in overview, overview
        missing = await connection.dispatch({"id": 3, "method": "session.family",
                                              "params": {"profile": "default", "session_id": "missing"}})
        assert missing["error"]["code"] == 4001
        assert set(authority.sessions) == {"root"}
        await connection.close()


async def _no_restore(*args):
    raise AssertionError("Family polling attempted cold adoption")


@pytest.mark.asyncio
@pytest.mark.parametrize("host_scoped", [False, True])
async def test_canonical_profile_grant_is_not_provenance(tmp_path, monkeypatch, host_scoped):
    import asyncio
    from contextlib import suppress
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    home = tmp_path / "home"
    other = home / "profiles" / "iris"
    other.mkdir(parents=True)
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    monkeypatch.setattr("gateway.session_local_adoption.adopt_before_restore", _no_restore)
    registry = SessionAuthorities(home, multiplexed=True)
    runner = SimpleNamespace(_draining=False, session_authorities=registry)
    with SessionDB(db_path=home / "state.db") as db, SessionDB(db_path=other / "state.db") as foreign:
        for store in (db, foreign):
            store._conn.executescript(LAUNCH_DDL)
        db.create_session("root", source="test")
        foreign.create_session("thread", source="tool")
        foreign.create_session("helper", source="subagent", parent_session_id="thread")
        launch(foreign._conn, "thread", kind="teammate", profile="iris")
        launch(foreign._conn, "helper", profile="iris", launcher="thread", launcher_profile="iris")
        foreign._conn.commit()
        authorities = []
        for path, store, name in ((home, db, "default"), (other, foreign, "iris")):
            authority = SessionAuthority(runner, profile_id=str(path), instance_id=name, db=store,
                epoch=begin_runtime_epoch(store, instance_id=name))
            registry.add(path, authority, name=None if name == "default" else name)
            authorities.append(authority)
        authorities[0].sessions["root"] = LiveSession(None, "root-route")
        task = asyncio.create_task(asyncio.Event().wait())
        authorities[1].sessions["thread"] = LiveSession(None, "thread-route", task=task)
        identity = {"user_id": "owner", "profile_scope": "host" if host_scoped else "profile"}
        connection = AuthorityConnection(authorities[0], object(), identity)
        try:
            result = await connection.dispatch({"id": 1, "method": "session.family",
                "params": {"profile": "default", "session_id": "root", "since": 0}})
            assert "result" in result, result
            family = result["result"]
            assert family["complete"] is host_scoped
            assert family["unavailable_profiles"] == ([] if host_scoped else ["iris"])
            assert {n["session_id"] for n in family["nodes"]} == ({"thread", "helper"} if host_scoped else set())
            overview = (await connection.dispatch({"id": 2, "method": "family.overview", "params": {}}))["result"]
            assert {p["profile"] for p in overview["team"]} == ({"default", "iris"} if host_scoped else {"default"})
            assert len(overview["roots"]) == (1 if host_scoped else 0)
            assert set(authorities[0].sessions) == {"root"}
            assert set(authorities[1].sessions) == {"thread"}
        finally:
            await connection.close()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_canonical_cold_stored_root_never_adopts_or_reads_another_local_owner(tmp_path, monkeypatch):
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    monkeypatch.setattr("gateway.session_local_adoption.adopt_before_restore", _no_restore)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("root", source="test")
        db.create_session("private", source="local")
        db._conn.execute("UPDATE sessions SET user_id='somebody-else' WHERE id='private'")
        db._conn.commit()
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home), instance_id="cold",
                                    db=db, epoch=begin_runtime_epoch(db, instance_id="cold"))
        connection = AuthorityConnection(authority, object(), {"user_id": "owner"})
        try:
            for sid, readable in (("root", True), ("private", False), ("missing", False)):
                result = await connection.dispatch({"id": 1, "method": "session.family",
                    "params": {"profile": "default", "session_id": sid}})
                assert ("result" in result) is readable, result
            assert not authority.sessions
        finally:
            await connection.close()



@pytest.mark.asyncio
async def test_canonical_pending_input_is_generation_bound_and_read_only(tmp_path, monkeypatch):
    import threading
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("root", source="test")
        db.create_session("child", source="subagent", parent_session_id="root")
        db._conn.executescript(LAUNCH_DDL)
        launch(db._conn, "child")
        db._conn.commit()
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home), instance_id="pending",
                                    db=db, epoch=begin_runtime_epoch(db, instance_id="pending"))
        live = LiveSession(None, "route")
        authority.sessions["child"] = live
        generation = db.get_session("child")["runtime_generation"]
        prompt = {"kind": "clarify", "prompt_id": "question", "execution_generation": generation}
        live.controls.pending["question"] = (SimpleNamespace(event=threading.Event()), prompt)
        before = db._conn.total_changes
        connection = AuthorityConnection(authority, object(), {"user_id": "owner"})
        try:
            request = {"id": 1, "method": "session.family", "params": {"profile": "default", "session_id": "root"}}
            result = (await connection.dispatch(request))["result"]["nodes"][0]
            assert result["status"] == "needs_you" and result["needs_you"]["kind"] == "clarify"
            assert "answer" in result["actions"]
            prompt["execution_generation"] += 1
            stale = (await connection.dispatch(request))["result"]["nodes"][0]
            assert stale["status"] == "unknown" and stale["needs_you"] is None
            assert list(live.controls.pending) == ["question"]
            assert db._conn.total_changes == before
            assert set(authority.sessions) == {"child"}
        finally:
            await connection.close()



@pytest.mark.asyncio
async def test_resident_execution_is_not_erased_by_an_idle_authority_handle(tmp_path, monkeypatch):
    import sys
    import threading
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("root", source="test")
        db.create_session("child", source="subagent", parent_session_id="root")
        db._conn.executescript(LAUNCH_DDL)
        launch(db._conn, "child")
        db._conn.commit()
        agent = SimpleNamespace(session_id="child", _session_db=db)
        registry = SimpleNamespace(_active_subagents_lock=threading.Lock(),
            _active_subagents={"child": {"agent": agent, "status": "running"}})
        monkeypatch.setitem(sys.modules, "tools.delegate_tool_registry", registry)
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home), instance_id="resident",
                                    db=db, epoch=begin_runtime_epoch(db, instance_id="resident"))
        authority.sessions["child"] = LiveSession(None, "route")
        connection = AuthorityConnection(authority, object(), {"user_id": "owner"})
        try:
            response = await connection.dispatch({"id": 1, "method": "session.family",
                "params": {"profile": "default", "session_id": "root"}})
            assert response["result"]["nodes"][0]["status"] == "working"
        finally:
            await connection.close()
