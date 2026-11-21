"""Unborn nested repositories are named snapshot gaps, never rollback targets."""
import json
import tempfile
from pathlib import Path, PurePosixPath

EXCLUDED_TRAILER = "Hermes-Uncaptured-Unborn: "


def unborn_paths(store: Path, working_dir: str, index_file: Path) -> list[str]:
    """Find only entirely untracked unborn repos: Git already handles tracked contents.

    `ls-files --others` emits a nested repository as `path/`, but emits actual
    filenames when its contents were already tracked before `git init`. Avoid
    walking every cached file (and changing the coverage of those directories).
    """
    from agent.checkpoints import manager as cm

    ok, names, err = cm._run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"],
        store, working_dir, index_file=index_file,
    )
    if not ok:
        raise OSError(f"Could not inspect untracked paths: {err}")
    root = Path(working_dir)
    excluded = []
    for name in sorted(set(names.split("\0"))):
        if not name.endswith("/"):
            continue
        path = root / name
        if not (path / ".git").exists() or not path.resolve().is_relative_to(root):
            continue
        # Detached HEAD and command failures retain Git's existing behavior.
        symbolic, _, _ = cm._run_git(
            ["symbolic-ref", "-q", "HEAD"], path / ".git", str(path),
            allowed_returncodes={1, 128},
        )
        if not symbolic:
            continue
        has_head, _, err = cm._run_git(
            ["rev-parse", "--verify", "--quiet", "HEAD"], path / ".git", str(path),
            allowed_returncodes={1, 128},
        )
        if not has_head and not err:
            excluded.append(path.relative_to(root).as_posix())
    return excluded


def stage_current(store: Path, working_dir: str, index_file: Path) -> tuple[bool, str]:
    """Diff/planning must still see oversized files, but not unborn repo gaps."""
    from agent.checkpoints import manager as cm

    excluded = unborn_paths(store, working_dir, index_file)
    args = cm._stage_all_args(store.parent, working_dir)
    if excluded:
        if "--" not in args:
            args.append("--")
        args.extend(exclusion_args(excluded))
    ok, _, err = cm._run_git(args, store, working_dir, index_file=index_file, timeout=cm._GIT_TIMEOUT * 2)
    return ok, err


def checkpoint_exclusions(store: Path, working_dir: str, commit_hash: str) -> list[str]:
    """Read the checkpoint-bound gap even after the nested .git marker disappears."""
    from agent.checkpoints import manager as cm

    ok, body, err = cm._run_git(["log", "-1", "--format=%B", commit_hash], store, working_dir)
    if not ok:
        raise OSError(f"Could not inspect checkpoint exclusions: {err}")
    trailer = next((line[len(EXCLUDED_TRAILER):] for line in reversed(body.splitlines())
                    if line.startswith(EXCLUDED_TRAILER)), None)
    if trailer is None:
        return []
    try:
        paths = json.loads(trailer)
    except ValueError as exc:
        raise OSError("Invalid checkpoint exclusions") from exc
    if not isinstance(paths, list) or any(
        not isinstance(p, str) or not p or PurePosixPath(p).is_absolute()
        or ".." in PurePosixPath(p).parts or p == "." for p in paths
    ):
        raise OSError("Invalid checkpoint exclusion paths")
    return paths


def restore_exclusions(store: Path, working_dir: str, commit_hash: str, tree: str) -> list[str]:
    from agent.checkpoints import manager as cm

    excluded = checkpoint_exclusions(store, working_dir, commit_hash)
    # The pre-rollback take normally repairs the project index. Inspect through
    # a fresh index first so a corrupt cache cannot block an otherwise valid restore.
    with tempfile.TemporaryDirectory(prefix="restore-gaps-", dir=store) as scratch:
        index = Path(scratch) / "index"
        tip = cm._ref_tip(store, working_dir, cm._ref_name(cm._project_hash(working_dir)))
        ok, _, err = cm._run_git(["read-tree", tip or "--empty"], store, working_dir, index_file=index)
        if not ok:
            raise OSError(f"Could not inspect restore gaps: {err}")
        excluded += unborn_paths(store, working_dir, index)
    # A formerly captured file at an ancestor must not replace the directory
    # now holding an uncaptured repository either.
    ancestors = [record.split("\t", 1)[1] for record in tree.split("\0") if "\t" in record
                 if any(p.startswith(record.split("\t", 1)[1] + "/") for p in excluded)]
    return sorted(set(excluded + ancestors))


def filter_selection(selected: list[str] | None, excluded: list[str]) -> list[str] | None:
    if selected is None:
        return None
    filtered = [p for p in selected if not contains_path(excluded, p)]
    if not filtered:
        raise OSError("Restore selection contains only uncaptured paths")
    return filtered


def warn_once(manager, working_dir: str, excluded: list[str]) -> None:
    from agent.checkpoints import manager as cm

    key = (working_dir, tuple(excluded))
    if excluded and key not in manager._warned_unborn:
        cm.logger.warning("Checkpoint does not capture unborn nested repositories: %s", json.dumps(excluded))
        manager._warned_unborn.add(key)


def _coverage_suffix(paths: list[str]) -> str:
    return " [uncaptured unborn repos kept: " + json.dumps(paths, ensure_ascii=False) + "]"


def disclose(reason: str, excluded: list[str]) -> str:
    """Use the existing rendered reason field on every rollback surface."""
    if not excluded:
        return reason
    reason = " ".join(reason.splitlines())
    suffix = _coverage_suffix(sorted(excluded))
    # Older snapshots sorted Git's directory entries before removing the slash.
    legacy_suffix = _coverage_suffix(sorted(excluded, key=lambda path: path + "/"))
    return reason if reason.endswith((suffix, legacy_suffix)) else reason + suffix


def contains_path(paths: list[str], rel: str) -> bool:
    return any(rel == path or rel.startswith(path + "/") for path in paths)


def exclusion_args(paths: list[str]) -> list[str]:
    return [f":(exclude,top,literal){path}" for path in paths]
