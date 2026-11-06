"""SKILL.md size law: skill_manage refuses a SKILL.md write that grows an owner-authored skill in
the shared library past its cap in ``<root>/laws/skills-budget.json``; references/ stay uncapped."""

import json
from unittest.mock import patch

import pytest

from tools.skill_manager_tool import skill_manage

CAP = 2_000


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skills = tmp_path / "skills"
    skills.mkdir()
    (tmp_path / "laws").mkdir()
    with patch("tools.skill_manager_tool.SKILLS_DIR", skills), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills]):
        yield tmp_path


def _budget(home, over_cap=None, **extra):
    (home / "laws" / "skills-budget.json").write_text(json.dumps(
        {"main_cap_bytes": CAP, "over_cap": over_cap or {}, **extra}))


def _content(name, body_bytes, author="Ava"):
    head = f"---\nname: {name}\ndescription: Use when testing the size law.\nauthor: {author}\n---\n\n# T\n\n"
    return head + "x" * (body_bytes - len(head) - 1) + "\n"


def _put(home, name, body_bytes, author="Ava"):
    d = home / "skills" / "ops" / name
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(_content(name, body_bytes, author))
    (d / "references" / "depth.md").write_text("# Depth\n\nold\n")
    return d


def _call(**kw):
    return json.loads(skill_manage(**kw))


def test_patch_growing_an_over_cap_skill_is_refused_and_points_at_references(home):
    _budget(home, over_cap={"big": 2_500})
    d = _put(home, "big", 2_500)
    before = (d / "SKILL.md").read_bytes()
    r = _call(action="patch", name="big", old_string="# T\n", new_string="# T\n\nOne more rule.\n")
    assert r["success"] is False
    assert "references/<topic>.md" in r["error"] and "2,500 B cap" in r["error"]
    assert (d / "SKILL.md").read_bytes() == before


def test_patch_to_a_reference_of_an_over_cap_skill_passes(home):
    _budget(home, over_cap={"big": 2_500})
    d = _put(home, "big", 2_500)
    r = _call(action="patch", name="big", file_path="references/depth.md",
              old_string="old\n", new_string="old\n" + "new depth\n" * 500)
    assert r["success"] is True
    assert "new depth" in (d / "references" / "depth.md").read_text()


def test_slimming_an_over_cap_skill_passes_even_while_still_over(home):
    _budget(home, over_cap={"big": 2_500})
    d = _put(home, "big", 2_500)
    r = _call(action="patch", name="big", old_string="# T\n\n" + "x" * 100, new_string="# T\n\n")
    assert r["success"] is True, r
    assert CAP < (d / "SKILL.md").stat().st_size < 2_500


def test_growth_up_to_the_cap_passes_and_past_it_is_refused(home):
    _budget(home)
    _put(home, "small", 1_000)
    assert _call(action="patch", name="small", old_string="# T\n", new_string="# T\n" + "y" * 900)["success"]
    r = _call(action="patch", name="small", old_string="# T\n", new_string="# T\n" + "z" * 200)
    assert r["success"] is False and "over its 2,000 B cap" in r["error"]


def test_full_rewrite_and_create_are_held_to_the_cap(home):
    _budget(home)
    _put(home, "small", 1_000)
    assert _call(action="patch", name="small", content=_content("small", 2_400))["success"] is False
    created = _call(action="create", name="fresh", category="ops", content=_content("fresh", 2_400))
    assert created["success"] is False and not (home / "skills" / "ops" / "fresh").exists()
    assert _call(action="create", name="tidy", category="ops", content=_content("tidy", 1_500))["success"]


def test_batch_refusal_rolls_back_the_whole_batch(home):
    _budget(home)
    d = _put(home, "small", 1_900)
    r = _call(action="patch", name="small", operations=[
        {"action": "write_file", "name": "small", "file_path": "references/more.md", "file_content": "# More\n"},
        {"action": "patch", "name": "small", "old_string": "# T\n", "new_string": "# T\n" + "w" * 300}])
    assert r["success"] is False
    assert not (d / "references" / "more.md").exists()


def test_other_authors_and_missing_budget_are_not_governed(home):
    _put(home, "vendor", 1_000, author="Upstream")
    _put(home, "mine", 1_000)
    grow = dict(old_string="# T\n", new_string="# T\n" + "v" * 3_000)
    assert _call(action="patch", name="mine", **grow)["success"] is True  # no budget file: no law
    _budget(home)
    assert _call(action="patch", name="vendor", **grow)["success"] is True
