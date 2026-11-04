"""Family Inbox rows carry the same folder identity as ordinary chat summaries."""
from types import SimpleNamespace

import pytest

from hermes_cli import projects_db as pdb
from hermes_state import SessionDB
from tests.test_hermes_family import NOW, launch


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["project", "workspace_only", None])
async def test_teammate_inbox_folder(tmp_path, monkeypatch, folder):
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    parent_path = str(tmp_path / "work")
    nested_path = str(tmp_path / "work" / "nested")
    cwd = str(tmp_path / "elsewhere") if folder == "workspace_only" else nested_path + "/src" if folder else None
    with pdb.connect_closing(home / "projects.db") as projects:
        parent_id = pdb.create_project(projects, name="Parent", folders=[parent_path])
        child_id = pdb.create_project(projects, name="Nested", folders=[nested_path])
        archived_id = pdb.create_project(projects, name="Archived", folders=[nested_path + "/src"])
        pdb.archive_project(projects, archived_id)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("root", source="test", cwd=parent_path)
        db.create_session("child", source="tool", cwd=cwd)
        launch(db._conn, "child", kind="teammate", cause="error", finished=NOW - 1)
        db._conn.commit()
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home),
            instance_id="folders", db=db, epoch=begin_runtime_epoch(db, instance_id="folders"))
        authority.sessions["root"] = LiveSession(None, "root-route")
        connection = AuthorityConnection(authority, object(), {"user_id": "owner"})
        try:
            # The shared contract is enforced by both native Family methods.
            family = await connection.dispatch({"id": 1, "method": "session.family", "params": {
                "profile": "default", "session_id": "root", "since": 0}})
            assert "result" in family, family
            overview = await connection.dispatch({"id": 2, "method": "family.overview", "params": {"since": 0}})
            assert "result" in overview, overview
            root = family["result"]["root"]
            assert (root["projectId"], root["workspace"]) == (parent_id, parent_path)
            for node in (family["result"]["nodes"][0], overview["result"]["failed"][0]):
                assert node["kind"] == "teammate"
                assert node["session_id"] == "child"
                assert node["root_session_id"] == "root"
                assert node["workspace"] == cwd
                assert node["projectId"] == (child_id if folder == "project" else None)
        finally:
            await connection.close()
