"""``GET /api/chat/workspaces`` ``aliases``: old long folder names (symlinks) per project root.

Workspace folders can be renamed to short names while the
old long names stay behind as symlinks in the same parent. Phones cannot see symlink targets
through ``fs/list``, so the picker learns the old names here, for name search only.
"""

import os

import pytest

from hermes_cli.web_routers import chat_workspaces


def _project(path, *, archived=False, extra=()):
    return {"primary_path": str(path), "archived": archived,
            "folders": [{"path": str(path), "is_primary": True}] + [{"path": str(p)} for p in extra]}


def test_aliases_are_symlinks_in_the_parent_that_resolve_to_the_project_root(tmp_path):
    parent = tmp_path / "hermes-workspaces"
    parent.mkdir()
    cloudseed, rm, seed = (parent / "ProjectA", parent / "ProjectB", parent / "ProjectC")
    for folder in (cloudseed, rm, seed):
        folder.mkdir()
    os.symlink(cloudseed, parent / "Example Workspace")
    os.symlink(rm, parent / "Example Client - Sample Project")
    os.symlink("ProjectB", parent / "relative-project")                  # relative link to the same root
    os.symlink(tmp_path, parent / "elsewhere")                 # points outside any project
    (parent / "notes.md").write_text("x")
    os.symlink(parent / "notes.md", parent / "notes-link")     # symlink to a file
    os.symlink(parent / "missing", parent / "dangling")        # broken link

    aliases = chat_workspaces._workspace_aliases([_project(cloudseed), _project(rm), _project(seed)])

    assert aliases == {
        str(cloudseed): ["Example Workspace"],
        str(rm): ["Example Client - Sample Project", "relative-project"],
    }


def test_archived_projects_relative_paths_and_unreadable_parents_are_skipped(tmp_path):
    root = tmp_path / "Personal"
    root.mkdir()
    os.symlink(root, tmp_path / "Personal Workspace")
    projects = [_project(root, archived=True), {"primary_path": "relative/path", "folders": []},
                _project(tmp_path / "gone" / "Ghost"), "not-a-dict", {"folders": None}]

    assert chat_workspaces._workspace_aliases(projects) == {}
    assert chat_workspaces._workspace_aliases([_project(root)]) == {str(root): ["Personal Workspace"]}


def test_secondary_folders_get_aliases_too_and_scan_is_bounded(tmp_path, monkeypatch):
    primary, secondary = tmp_path / "Project", tmp_path / "Project-assets"
    primary.mkdir(); secondary.mkdir()
    os.symlink(secondary, tmp_path / "example.invalid assets")
    assert chat_workspaces._workspace_aliases([_project(primary, extra=[secondary])]) == {
        str(secondary): ["example.invalid assets"]}
    monkeypatch.setattr(chat_workspaces, "_ALIAS_SCAN_LIMIT", 0)
    assert chat_workspaces._workspace_aliases([_project(primary, extra=[secondary])]) == {}


pytest.importorskip("starlette.testclient")


def test_endpoint_returns_aliases_additively(tmp_path):
    from starlette.testclient import TestClient

    from hermes_cli import projects_db as pdb
    from hermes_cli import web_server

    root = tmp_path / "ProjectA"
    root.mkdir()
    os.symlink(root, tmp_path / "Example Workspace")
    with pdb.connect_closing() as conn:
        pdb.create_project(conn, name="ProjectA", folders=[str(root)])

    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    try:
        client = TestClient(web_server.app)
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        body = client.get("/api/chat/workspaces").json()
    finally:
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous

    assert body["aliases"] == {str(root): ["Example Workspace"]}
    assert [p["name"] for p in body["projects"]] == ["ProjectA"]
