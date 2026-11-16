"""Unborn nested repositories are named snapshot gaps, never rollback targets."""
import json
from pathlib import Path, PurePosixPath

EXCLUDED_TRAILER = "Hermes-Uncaptured-Unborn: "


def unborn_paths(store: Path, working_dir: str, index_file: Path) -> list[str]:
    """Discover only Git-visible nested repos; read their refs without changing them.

    Cached files can predate a nested `git init`, so inspect their ancestors too.
    Do not walk ignored dependencies or follow symlinks outside the worktree.
    """
    from tools import checkpoint_manager as cm

    ok, names, err = cm._run_git(
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        store, working_dir, index_file=index_file,
    )
    if not ok:
        raise OSError(f"Could not inspect nested repositories: {err}")
    root = Path(working_dir)
    candidates = set()
    inspected = set()
    for name in filter(None, names.split("\0")):
        path = root / name
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent in inspected:
                break
            inspected.add(parent)
            if parent.is_symlink():
                continue
            if (parent / ".git").exists() and parent.resolve().is_relative_to(root):
                candidates.add(parent)
    excluded = []
    for path in sorted(candidates):
        # Detached HEAD, broken repositories, and command failures retain Git's
        # existing fail-closed behavior. Only a valid symbolic, absent HEAD is unborn.
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
    from tools import checkpoint_manager as cm

    excluded = unborn_paths(store, working_dir, index_file)
    if not drop_from_index(store, working_dir, index_file, excluded):
        return False, "could not drop uncaptured nested repositories"
    args = cm._stage_all_args(store.parent, working_dir)
    if excluded:
        if "--" not in args:
            args.append("--")
        args.extend(exclusion_args(excluded))
    ok, _, err = cm._run_git(args, store, working_dir, index_file=index_file, timeout=cm._GIT_TIMEOUT * 2)
    return ok, err


def drop_from_index(store: Path, working_dir: str, index_file: Path, paths: list[str]) -> bool:
    """Forget previously captured contents as well as excluding new additions."""
    from tools import checkpoint_manager as cm

    if not paths:
        return True
    ok, _, _ = cm._run_git(
        ["rm", "--cached", "-r", "-f", "-q", "--ignore-unmatch", "--", *paths],
        store, working_dir, index_file=index_file,
        extra_env={"GIT_LITERAL_PATHSPECS": "1"},
    )
    return ok


def checkpoint_exclusions(store: Path, working_dir: str, commit_hash: str) -> list[str]:
    """Read the checkpoint-bound gap even after the nested .git marker disappears."""
    from tools import checkpoint_manager as cm

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
    from tools import checkpoint_manager as cm

    excluded = checkpoint_exclusions(store, working_dir, commit_hash)
    excluded += unborn_paths(store, working_dir, cm._index_path(store, cm._project_hash(working_dir)))
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
    from tools import checkpoint_manager as cm

    key = (working_dir, tuple(excluded))
    if excluded and key not in manager._warned_unborn:
        cm.logger.warning("Checkpoint does not capture unborn nested repositories: %s", json.dumps(excluded))
        manager._warned_unborn.add(key)


def contains_path(paths: list[str], rel: str) -> bool:
    return any(rel == path or rel.startswith(path + "/") for path in paths)


def exclusion_args(paths: list[str]) -> list[str]:
    return [f":(exclude,top,literal){path}" for path in paths]
