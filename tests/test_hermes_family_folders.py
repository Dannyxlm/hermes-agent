"""Folder metadata stays profile-local, nullable on old stores, and follows compression tips."""
import sqlite3

import pytest

from hermes_cli import projects_db as pdb
from hermes_family import Family, read_profile
from hermes_state import SessionDB
from tests.test_hermes_family import NOW, fixture_db, launch, session


@pytest.mark.parametrize("project_store", ["absent", "old", "corrupt"])
def test_legacy_family_unknown_folder_is_null_without_bootstrap(tmp_path, project_store):
    path = tmp_path / "state.db"
    with fixture_db(path) as db:
        session(db, "root")
        session(db, "child")
        launch(db, "child", kind="teammate", cause="error", finished=NOW - 1)
    projects = tmp_path / "projects.db"
    if project_store == "old":
        with sqlite3.connect(projects) as conn:
            conn.execute("CREATE TABLE legacy (id TEXT)")
    elif project_store == "corrupt":
        projects.write_bytes(b"not a database")
    before = projects.read_bytes() if projects.exists() else None
    family = Family({"default": read_profile("default", path)}, now=NOW)
    node = family.overview(since=0)["failed"][0]
    assert node["projectId"] is None and node["workspace"] is None
    assert (projects.read_bytes() if projects.exists() else None) == before


def test_family_folder_uses_own_profile_and_compression_tip(tmp_path):
    profiles, expected = {}, {}
    for profile in ("default", "iris"):
        home = tmp_path / profile
        home.mkdir()
        cwd = str(tmp_path / "folder")
        with pdb.connect_closing(home / "projects.db") as conn:
            expected[profile] = pdb.create_project(conn, name=profile, folders=[cwd])
        with SessionDB(db_path=home / "state.db") as db:
            db.create_session("root", source="test")
            db.create_session("child", source="cli", cwd=str(tmp_path / "old"))
            db.create_session("tip", source="cli", parent_session_id="child", cwd=cwd)
            db._conn.execute("UPDATE sessions SET end_reason='compression' WHERE id='child'")
            launch(db._conn, "child", profile=profile, launcher_profile=profile,
                   kind="teammate", cause="error", finished=NOW - 1)
            db._conn.commit()
        profiles[profile] = read_profile(profile, home / "state.db")
    family = Family(profiles, now=NOW)
    nodes = family.overview(since=0)["failed"]
    assert len(nodes) == 2
    for node in nodes:
        assert node["session_id"] == "tip"
        assert node["workspace"] == cwd
        assert node["projectId"] == expected[node["profile"]]
