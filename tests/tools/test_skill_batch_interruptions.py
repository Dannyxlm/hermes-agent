"""In-process interruption must not leave a partially applied skill batch."""
import asyncio
import json

import pytest

from tools import skill_manager_tool as smt


SKILL = "---\nname: {name}\ndescription: Use when testing batch recovery.\n---\n# Test\nOriginal.\n"


def call(operations):
    return json.loads(smt.skill_manage(action="", name="", operations=operations))


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_interrupted_write_rolls_back_or_preserves_recovery_copy(
    tmp_path, monkeypatch, interruption, rollback_fails,
):
    from tools import skill_manager_batch as batch

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    skills = tmp_path / "skills"
    skills.mkdir()
    assert call([{"name": "alpha", "action": "create", "content": SKILL.format(name="alpha")}])["success"]
    original = (skills / "alpha" / "SKILL.md").read_bytes()
    adopted = skills / "adopted"
    adopted.mkdir()
    snapshots = tmp_path / "batch-snapshots"
    real_mkdtemp = batch.tempfile.mkdtemp
    real_copytree = batch.shutil.copytree
    real_write = smt.atomic_write_text
    signal = interruption("synthetic interruption")

    def create_snapshots(*args, **kwargs):
        if kwargs.get("prefix") == "skill_batch_":
            snapshots.mkdir()
            return str(snapshots)
        return real_mkdtemp(*args, **kwargs)

    def interrupt_after_write(target, *args, **kwargs):
        result = real_write(target, *args, **kwargs)
        if target.name == "interrupted.md":
            (adopted / "outside.txt").write_text("not a batch write")
            raise signal
        return result

    def fail_only_recovery(src, dst, *args, **kwargs):
        if rollback_fails and str(src).startswith(str(snapshots)):
            raise OSError("synthetic recovery-copy failure")
        return real_copytree(src, dst, *args, **kwargs)

    operations = [
        {"name": "alpha", "action": "patch", "old_string": "Original.", "new_string": "Changed."},
        {"name": "fresh", "action": "create", "content": SKILL.format(name="fresh")},
        {"name": "adopted", "action": "create", "content": SKILL.format(name="adopted")},
        {"name": "adopted", "action": "write_file", "file_path": "references/interrupted.md", "file_content": "partial"},
        {"name": "later", "action": "create", "content": SKILL.format(name="later")},
    ]
    with monkeypatch.context() as scope:
        scope.setattr(batch.tempfile, "mkdtemp", create_snapshots)
        scope.setattr(batch.shutil, "copytree", fail_only_recovery)
        scope.setattr(smt, "atomic_write_text", interrupt_after_write)
        with pytest.raises(interruption) as raised:
            call(operations)
    assert raised.value is signal
    if rollback_fails:
        assert snapshots.is_dir()
        assert (snapshots / "alpha" / "SKILL.md").read_bytes() == original
        # A failed recovery still leaves the half-applied skill, never an empty hole.
        assert (skills / "alpha" / "SKILL.md").is_file()
    else:
        assert (skills / "alpha" / "SKILL.md").read_bytes() == original
        assert not snapshots.exists()
    assert not (skills / "fresh").exists()
    assert not (skills / "later").exists()
    assert sorted(p.relative_to(adopted).as_posix() for p in adopted.rglob("*")) == ["outside.txt"]
    assert not smt._skill_gate_bypass.get()
    assert not smt._skill_review_batch.get()
    # All locks and context tokens must permit a later independent batch.
    assert call([{"name": "recovered", "action": "create", "content": SKILL.format(name="recovered")}])["success"]
