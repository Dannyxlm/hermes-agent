"""Checkpoint reasons must not repeat an already-rendered coverage warning."""
import json
import os
import subprocess

import pytest

from tools import checkpoint_manager as cm
from tools.checkpoint_unborn import disclose


@pytest.mark.parametrize("safe,file_path", [(False, None), (True, None), (False, "notes.txt")])
def test_restore_does_not_repeat_saved_coverage_warning(tmp_path, monkeypatch, safe, file_path):
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    work = tmp_path / "work"
    child = work / "new repo"
    child.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(child)], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    draft = child / "draft.txt"
    draft.write_text("not captured\n")
    notes = work / "notes.txt"
    notes.write_text("before\n")
    manager = cm.CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(str(work), "baseline")
    entry = manager.list_checkpoints(str(work))[0]
    notes.write_text("after\n")
    manager.record_agent_write(str(notes))
    result = manager.restore(str(work), entry["hash"], safe=safe, file_path=file_path)
    assert result["success"], result
    assert notes.read_text() == "before\n"
    assert draft.read_text() == "not captured\n"
    assert result["reason"] == entry["reason"]
    assert result["reason"].count("uncaptured unborn repos kept") == 1


def test_restore_does_not_repeat_coverage_for_prefix_siblings(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    work = tmp_path / "work"
    git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    drafts = []
    for name in ("app", "app-old"):
        child = work / name
        child.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(child)], check=True, capture_output=True, env=git_env)
        draft = child / "draft.txt"
        draft.write_text(name)
        drafts.append(draft)
    notes = work / "notes.txt"
    notes.write_text("before\n")
    manager = cm.CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(str(work), "baseline")
    entry = manager.list_checkpoints(str(work))[0]
    notes.write_text("after\n")
    manager.record_agent_write(str(notes))

    result = manager.restore(str(work), entry["hash"], safe=False)

    assert result["success"], result
    assert result["reason"] == entry["reason"]
    assert result["reason"].count("uncaptured unborn repos kept") == 1
    assert notes.read_text() == "before\n"
    assert [draft.read_text() for draft in drafts] == ["app", "app-old"]


def test_disclosure_recognizes_legacy_git_listing_order():
    saved = 'baseline [uncaptured unborn repos kept: ["app-old", "app"]]'
    assert disclose(saved, ["app", "app-old"]) == saved


def test_disclosure_keeps_changed_gaps_and_unrelated_reason_text():
    gaps = ['new "repo"', "雪", "line\nbreak"]
    original = "reason with [brackets] and arbitrary suffix"
    once = disclose(original, gaps)
    assert disclose(once, gaps) == once
    assert json.dumps(sorted(gaps), ensure_ascii=False) in once
    assert once.startswith(original)
    expanded = disclose(once, [*gaps, "another"])
    assert json.dumps(sorted([*gaps, "another"]), ensure_ascii=False) in expanded
    assert expanded.startswith(original)
    assert disclose(original, []) == original
