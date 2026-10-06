"""Background-review creates meet a one-time overlap check against installed skills.

Regression: one long conversation's review forks minted four near-identical skills
(personal-document-vault, personal-records-vault, personal-identity-documents,
personal-identity-paperwork) because each fork's skill index predated its siblings' creates and the
create path only rejected exact-name duplicates.
"""

import json

import pytest

EXISTING = """---
name: personal-records-vault
description: "Use when filing the owner's ID, insurance or lease documents."
---

# Personal records vault

## When to Use

File a passport, driver's licence, health card or insurance policy into 1Password and write a
Dbrain pointer without the document number. Use local OCR and MRZ check digits.
"""

UNRELATED = """---
name: kubernetes-rollouts
description: "Use when rolling out Kubernetes deployments."
---

# Kubernetes rollouts

Canary a deployment with kubectl, watch pod readiness and roll back on failed probes.
"""

NEAR_DUPLICATE = """---
name: personal-identity-paperwork
description: "Use when filing the owner's IDs or government paperwork."
---

# Personal identity paperwork

## When to Use

The owner shares a passport, licence, health card or insurance policy to file. Save numbers in
1Password, a Dbrain pointer without the number, validate MRZ check digits with local OCR.
"""


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    from agent import skill_utils
    from tools import skill_ledger, skill_manager_tool, skill_usage

    home = tmp_path / "home"
    skills_dir = home / "skills"
    for name, body in (("personal-records-vault", EXISTING), ("kubernetes-rollouts", UNRELATED)):
        (skills_dir / name).mkdir(parents=True)
        (skills_dir / name / "SKILL.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(skill_ledger, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_usage, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skills_dir])
    return skills_dir


def _create(name, content):
    from tools.skill_manager_tool import skill_manage

    return json.loads(skill_manage(action="create", name=name, content=content))


def _in_review(fn):
    from tools.skill_manager_guards import _reset_background_review_read_marks
    from tools.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        _reset_background_review_read_marks()
        return fn()
    finally:
        reset_current_write_origin(token)


def test_review_create_is_first_shown_the_closest_skill_then_allowed_on_repeat(skills_env):
    def run():
        first = _create("personal-identity-paperwork", NEAR_DUPLICATE)
        created_after_first = (skills_env / "personal-identity-paperwork").exists()
        second = _create("personal-identity-paperwork", NEAR_DUPLICATE)
        return first, created_after_first, second

    first, created_after_first, second = _in_review(run)
    assert first["success"] is False
    assert [m["name"] for m in first["overlap_candidates"]][0] == "personal-records-vault"
    assert "kubernetes-rollouts" not in first["error"]
    assert created_after_first is False
    assert second["success"] is True


def test_foreground_create_is_not_slowed_by_the_overlap_check(skills_env):
    result = _create("personal-identity-paperwork", NEAR_DUPLICATE)
    assert result["success"] is True
