"""Family reads use the existing RPC dispatchers and do not restore runtimes."""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.test_hermes_family import LAUNCH_DDL, launch, session, NOW


@pytest.mark.asyncio
async def test_canonical_family_entrypoint(tmp_path, monkeypatch):
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
        launch(db._conn, "child", cause="success", finished=NOW - 1)
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
        overview = await connection.dispatch({"id": 2, "method": "family.overview", "params": {"since": 0}})
        assert "result" in overview, overview
        missing = await connection.dispatch({"id": 3, "method": "session.family",
                                              "params": {"profile": "default", "session_id": "missing"}})
        assert missing["error"]["code"] == 4001
        assert set(authority.sessions) == {"root"}
        await connection.close()


async def _no_restore(*args):
    raise AssertionError("Family polling attempted cold adoption")
