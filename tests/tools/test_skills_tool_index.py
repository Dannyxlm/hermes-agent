"""Lookup freshness and filesystem work contracts for the in-process skill index."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import skills_tool as st


def _skill(root, directory, name=None, body="Instructions."):
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name or directory}\ndescription: Use when testing.\n---\n\n{body}\n")
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(st, "SKILLS_DIR", root)
    monkeypatch.setattr("agent.skill_utils.get_project_skills_dirs", list)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", list)
    st.clear_skills_cache()
    yield root
    st.clear_skills_cache()


def _lookup(root, name):
    return st.locate_skill(name, None, [(1, root)])


def test_repeat_lookup_does_not_walk_the_tree(library, monkeypatch):
    target = _skill(library, "category/package", "public-name")
    for i in range(12):
        _skill(library, f"category/filler-{i}")
    walks, scans = [], []
    real_walk, real_scan = os.walk, os.scandir

    def walk(*args, **kwargs):
        walks.append(args[0])
        return real_walk(*args, **kwargs)

    def scan(*args, **kwargs):
        if Path(args[0]).is_relative_to(library):
            scans.append(args[0])
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(os, "scandir", scan)
    assert _lookup(library, "public-name") == (None, target.parent, target)
    assert scans
    cold_scans = scans.copy()
    walks.clear()
    scans.clear()
    assert _lookup(library, "public-name") == (None, target.parent, target)
    assert walks == [] and scans == [], "warm lookup must validate candidates, not rescan the library"
    expected_dirs = {str(library), str(target.parent.parent), str(target.parent)}
    expected_dirs.update(str(library / "category" / f"filler-{i}") for i in range(12))
    assert set(map(str, cold_scans)) == expected_dirs
    assert len(cold_scans) == len(expected_dirs), "build every index in ONE tree walk"


def test_disk_create_and_directory_rename_resolve_without_waiting(library):
    _skill(library, "seed")
    assert _lookup(library, "seed")[0] is None
    created = _skill(library, "category/new-package", "new-public-name")
    assert _lookup(library, "new-public-name") == (None, created.parent, created)
    renamed = created.parent.with_name("moved-package")
    created.parent.rename(renamed)
    assert _lookup(library, "new-public-name") == (None, renamed, renamed / "SKILL.md")
    assert _lookup(library, "moved-package")[1] == renamed


def test_deleted_candidate_is_not_returned(library):
    path = _skill(library, "category/package", "public-name")
    assert _lookup(library, "public-name")[2] == path
    path.unlink()
    error, directory, found = _lookup(library, "public-name")
    assert json.loads(error)["success"] is False
    assert directory is None and found is None


def test_edited_frontmatter_invalidates_old_alias_before_new_alias_lookup(library):
    path = _skill(library, "package", "old-public-name")
    assert _lookup(library, "old-public-name")[2] == path
    _skill(library, "package", "new-public-name")
    assert json.loads(_lookup(library, "old-public-name")[0])["success"] is False
    assert _lookup(library, "new-public-name")[2] == path


def test_duplicates_in_snapshot_are_refused(library):
    first = _skill(library, "category/first", "shared", "First instructions.")
    second = _skill(library, "other/second", "shared", "Different instructions.")
    for _ in range(2):
        result = json.loads(_lookup(library, "shared")[0])
        assert result["success"] is False
        assert set(result["matches"]) == {str(first), str(second)}
        assert "refusing to guess" in result["error"]


def test_ttl_exposes_an_out_of_process_duplicate(library, monkeypatch):
    from tools import skills_tool_index as index

    clock = [0.0]
    monkeypatch.setattr(index, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    first = _skill(library, "first", "shared", "First instructions.")
    assert _lookup(library, "shared")[2] == first
    second = _skill(library, "second", "shared", "Different instructions.")
    # A still-valid cached hit cannot discover out-of-process duplicates without
    # walking again. That staleness is deliberately bounded by the snapshot TTL.
    assert _lookup(library, "shared")[2] == first
    clock[0] += index._TTL_SECONDS + 1
    result = json.loads(_lookup(library, "shared")[0])
    assert set(result["matches"]) == {str(first), str(second)}


def test_legacy_index_keeps_order_and_rejects_package_owned_markdown(library):
    direct = library / "target.md"
    direct.write_text("Direct legacy skill.")
    first = _skill(library, "a/package", "target")
    by_dir = _skill(library, "b/target", "other-name")
    nested = library / "legacy" / "target.md"
    nested.parent.mkdir()
    nested.write_text("Nested legacy skill.")
    (first.parent / "target.md").write_text("Package document, not a skill.")
    support = first.parent / "references" / "target.md"
    support.parent.mkdir()
    support.write_text("Reference, not a skill.")
    expected = [(None, direct), (first.parent, first), (by_dir.parent, by_dir), (None, nested)]
    for _ in range(2):
        assert st._collect_skill_candidates("target", None, [library]) == expected


@pytest.mark.parametrize("action", ["create", "patch", "edit", "write_file", "remove_file", "delete"])
def test_skill_manage_mutations_invalidate_warm_index(library, monkeypatch, action):
    from tools import skill_manager_tool as manager

    target = _skill(library, "package")
    _skill(library, "seed")
    support = target.parent / "references" / "example.md"
    support.parent.mkdir()
    support.write_text("Reference.")
    monkeypatch.setattr(manager, "SKILLS_DIR", library)
    monkeypatch.setattr(manager, "_apply_skill_write_gate", lambda *a, **kw: None)
    assert _lookup(library, "seed")[0] is None
    args = {
        "create": dict(name="created", content="---\nname: created\ndescription: Use when testing.\n---\n\nInstructions.\n"),
        "patch": dict(old_string="Instructions.", new_string="Updated instructions."),
        "edit": dict(content="---\nname: package\ndescription: Use when testing.\n---\n\nRewritten.\n"),
        "write_file": dict(file_path="references/new.md", file_content="New reference."),
        "remove_file": dict(file_path="references/example.md"),
        "delete": {},
    }[action]
    result = json.loads(manager.skill_manage(action=action, **{"name": "package", **args}))
    assert result["success"] is True, result
    walks = []
    real_scan = os.scandir

    def scan(*a, **kw):
        walks.append(a[0])
        return real_scan(*a, **kw)

    monkeypatch.setattr(os, "scandir", scan)
    assert _lookup(library, "seed")[0] is None
    assert walks, f"{action} must invalidate even an unrelated cached hit"


def test_recursive_and_legacy_candidate_order_matches_existing_walkers(library):
    from agent.skill_utils import iter_skill_index_files

    _skill(library, "a/package", "shared", "One.")
    _skill(library, "a-b/package", "shared", "Two.")
    for relative in ("a/target.md", "a/deep/target.md", "b/target.md"):
        path = library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Legacy instructions.")
    expected_skills = [(p.parent, p) for p in iter_skill_index_files(library, "SKILL.md")]
    expected_legacy = [(None, p) for p in library.rglob("target.md")]
    assert st._collect_skill_candidates("shared", None, [library]) == expected_skills
    assert st._collect_skill_candidates("target", None, [library]) == expected_legacy
    assert st._collect_skill_candidates("target", None, [library]) == expected_legacy


@pytest.mark.parametrize("name, expected", [("skills/demo", []),
                                           ("category/**/demo", ["category/a/b/demo.md"])])
def test_legacy_patterns_are_root_relative_and_recursive(library, name, expected):
    (library / "demo.md").write_text("Root legacy.")
    path = library / "category/a/b/demo.md"
    path.parent.mkdir(parents=True)
    path.write_text("Deep legacy.")
    assert st._collect_skill_candidates(name, None, [library]) == [(None, library / p) for p in expected]


def test_embedded_star_legacy_pattern_matches_pathlib(library):
    path = library / "category/demo.md"
    path.parent.mkdir()
    path.write_text("Legacy instructions.")
    name = "category/de**"
    try:
        expected = [(None, p) for p in library.rglob(f"{name}.md")]
    except ValueError:  # pathlib before 3.13 rejects embedded '**'
        with pytest.raises(ValueError):
            st._collect_skill_candidates(name, None, [library])
    else:
        assert st._collect_skill_candidates(name, None, [library]) == expected


@pytest.mark.platforms("posix")
def test_explicit_legacy_component_can_follow_symlink(library, tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    (target / "demo.md").write_text("Linked legacy.")
    category = library / "category"
    category.mkdir()
    (category / "link").symlink_to(target, target_is_directory=True)
    assert st._collect_skill_candidates("link/demo", None, [library]) == [(None, category / "link/demo.md")]
    assert st._collect_skill_candidates("demo", None, [library]) == []


def test_candidate_that_becomes_support_content_is_not_returned(library):
    path = _skill(library, "package/references/example", "example")
    assert _lookup(library, "example")[2] == path
    _skill(library, "package")
    assert _lookup(library, "example")[2] is None


def test_partial_curator_move_invalidates_even_when_it_fails(library, monkeypatch):
    from tools import skill_usage

    _skill(library, "first", "shared", "First instructions.")
    source = _skill(library, ".archive/restored", "shared", "Different instructions.").parent
    monkeypatch.setattr(skill_usage, "_skills_dir", lambda: library)
    assert _lookup(library, "shared")[0] is None
    rename = Path.rename

    def cross_device(path, destination):
        if path == source:
            raise OSError("cross-device move")
        return rename(path, destination)

    def partial_move(src, dest):
        _skill(library, "restored", "shared", "Different instructions.")
        raise OSError("support-file copy failed after SKILL.md")

    monkeypatch.setattr(Path, "rename", cross_device)
    monkeypatch.setattr("shutil.move", partial_move)
    ok, message = skill_usage.restore_skill("restored")
    assert not ok and "failed to restore" in message
    assert "refusing to guess" in json.loads(_lookup(library, "shared")[0])["error"]


def test_curator_archive_and_restore_invalidate(library, monkeypatch):
    from tools import skill_usage

    _skill(library, "package")
    _skill(library, "seed")
    monkeypatch.setattr(skill_usage, "_skills_dir", lambda: library)
    assert _lookup(library, "seed")[0] is None
    real_scan = os.scandir
    walks = []

    def scan(*a, **kw):
        walks.append(a[0])
        return real_scan(*a, **kw)

    monkeypatch.setattr(os, "scandir", scan)
    for relocate in (skill_usage.archive_skill, skill_usage.restore_skill):
        ok, message = relocate("package")
        assert ok, message
        walks.clear()
        assert _lookup(library, "seed")[0] is None
        assert walks, "curator moves must invalidate cached hits"
