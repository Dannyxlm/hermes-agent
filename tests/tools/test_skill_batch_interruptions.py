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


@pytest.mark.parametrize("escape", ["parent", pytest.param("symlink", marks=pytest.mark.platforms("posix"))])
def test_interrupted_failure_recovery_never_unlinks_rejected_external_path(tmp_path, monkeypatch, escape):
    from tools import skill_manager_batch as batch

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    skills = tmp_path / "skills"
    skills.mkdir()
    assert call([{"name": "alpha", "action": "create", "content": SKILL.format(name="alpha")}])["success"]
    original = (skills / "alpha" / "SKILL.md").read_bytes()
    adopted = skills / "adopted"
    adopted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("preserve")
    real_copytree = batch.shutil.copytree
    real_from = smt._skill_manage_from
    interrupted = False
    signal = KeyboardInterrupt("synthetic interruption during recovery")

    def interrupt_first_recovery(src, dst, *args, **kwargs):
        nonlocal interrupted
        if str(src).startswith(str(tmp_path / "skill_batch_")) and not interrupted:
            interrupted = True
            raise signal
        return real_copytree(src, dst, *args, **kwargs)

    def create_external_redirect(payload, **kwargs):
        if payload.get("action") == "write_file" and escape == "symlink":
            (adopted / "references").symlink_to(outside, target_is_directory=True)
        return real_from(payload, **kwargs)

    real_mkdtemp = batch.tempfile.mkdtemp

    def local_snapshots(*args, **kwargs):
        if kwargs.get("prefix") == "skill_batch_":
            kwargs["dir"] = tmp_path
        return real_mkdtemp(*args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(batch.tempfile, "mkdtemp", local_snapshots)
        scope.setattr(batch.shutil, "copytree", interrupt_first_recovery)
        scope.setattr(smt, "_skill_manage_from", create_external_redirect)
        with pytest.raises(KeyboardInterrupt) as raised:
            call([
                {"name": "alpha", "action": "patch", "old_string": "Original.", "new_string": "Changed."},
                {"name": "adopted", "action": "create", "content": SKILL.format(name="adopted")},
                {"name": "adopted", "action": "write_file", "file_path": (
                    "../../outside/victim.txt" if escape == "parent" else "references/victim.txt"
                ), "file_content": "never written"},
            ])
    assert interrupted and raised.value is signal
    assert victim.read_text() == "preserve"
    assert (skills / "alpha" / "SKILL.md").read_bytes() == original
    assert not smt._skill_gate_bypass.get() and not smt._skill_review_batch.get()


def test_second_interruption_preserves_original_recovery_snapshot(tmp_path, monkeypatch):
    from tools import skill_manager_batch as batch

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    (tmp_path / "skills").mkdir()
    assert call([{"name": "alpha", "action": "create", "content": SKILL.format(name="alpha")}])["success"]
    original = (tmp_path / "skills" / "alpha" / "SKILL.md").read_bytes()
    snapshots = tmp_path / "snapshots"
    real_write, real_copytree = smt.atomic_write_text, batch.shutil.copytree
    first, second = KeyboardInterrupt("first"), KeyboardInterrupt("second")

    def snapshot_dir(**kwargs):
        snapshots.mkdir()
        return str(snapshots)

    def interrupt_write(target, *args, **kwargs):
        result = real_write(target, *args, **kwargs)
        if target.name == "interrupted.md":
            raise first
        return result

    def interrupt_recovery(src, dst, *args, **kwargs):
        if str(src).startswith(str(snapshots)):
            raise second
        return real_copytree(src, dst, *args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(batch.tempfile, "mkdtemp", snapshot_dir)
        scope.setattr(smt, "atomic_write_text", interrupt_write)
        scope.setattr(batch.shutil, "copytree", interrupt_recovery)
        with pytest.raises(KeyboardInterrupt) as raised:
            call([
                {"name": "alpha", "action": "patch", "old_string": "Original.", "new_string": "Changed."},
                {"name": "alpha", "action": "write_file", "file_path": "references/interrupted.md", "file_content": "partial"},
            ])
    assert raised.value is second and raised.value.__context__ is first
    assert (snapshots / "alpha" / "SKILL.md").read_bytes() == original
    assert not smt._skill_gate_bypass.get() and not smt._skill_review_batch.get()
