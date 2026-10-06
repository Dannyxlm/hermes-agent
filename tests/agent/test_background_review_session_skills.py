"""The background review prompt lists skills already written in the same conversation.

Regression: a review fork reuses the parent's cached system prompt (skill index frozen at session
start) and replays a transcript that holds no trace of earlier review forks, so successive reviews
in one conversation each created a sibling of the skill the previous review had just created.
"""

import json

import pytest

SKILL = """---
name: {name}
description: test skill {name}
---

# {name}

Body.
"""


@pytest.fixture
def ledger_env(tmp_path, monkeypatch):
    from agent import skill_utils
    from tools import skill_ledger, skill_manager_tool, skill_usage

    home = tmp_path / "home"
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)
    monkeypatch.setattr(skill_ledger, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_usage, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skills_dir])
    return skills_dir


def _review_create(name, session_id):
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        return json.loads(skill_manage(action="create", name=name, content=SKILL.format(name=name),
                                       session_id=session_id))
    finally:
        reset_current_write_origin(token)


def test_block_names_skills_created_earlier_in_this_session_only(ledger_env):
    from agent.background_review_skills import session_skill_activity_block

    assert _review_create("personal-document-vault", "chat-a")["success"]
    assert _review_create("unrelated-elsewhere", "chat-b")["success"]

    block = session_skill_activity_block(("chat-a", None))

    assert "personal-document-vault" in block
    assert "created by a background review" in block
    assert "unrelated-elsewhere" not in block
    assert session_skill_activity_block(("chat-c", None)) == ""


def test_spawned_skill_review_prompt_carries_the_session_block(ledger_env, monkeypatch):
    from agent import background_review

    assert _review_create("personal-document-vault", "chat-a")["success"]
    seen = {}
    monkeypatch.setattr(background_review, "_run_review_in_thread",
                        lambda agent, snapshot, prompt, **kw: seen.setdefault("prompt", prompt))

    class _Agent:
        session_id = "chat-a"
        _parent_session_id = None

    target, base_prompt = background_review.spawn_background_review_thread(
        _Agent(), [], review_skills=True, task_cfg={})
    target()

    assert seen["prompt"].startswith(base_prompt)
    assert "personal-document-vault" in seen["prompt"]
