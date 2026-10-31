"""Short-lived, per-search-root snapshots for skill name resolution (not the catalog)."""

import fnmatch
import os
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS

_TTL_SECONDS = 5.0
_MAX_ROOTS = 128
_GENERATION = 0
_LOCK = threading.Lock()
_SNAPSHOTS: dict[Path, "_Snapshot"] = {}


@dataclass
class _Snapshot:
    generation: int
    built_at: float = 0.0
    skills: list[Path] = field(default_factory=list)
    by_directory: dict[str, list[int]] = field(default_factory=dict)
    by_name: dict[str, list[int]] = field(default_factory=dict)
    flat: dict[str, list[Path]] = field(default_factory=dict)
    legacy: set[Path] = field(default_factory=set)
    # Entry name / is-directory / is-symlink, in native scandir order.
    tree: dict[Path, list[tuple[str, bool, bool]]] = field(default_factory=dict)


def _walk_tree(root):
    """One scandir per directory, following links as iter_skill_index_files does."""
    pending = [(root, True)]
    while pending:
        base, skill_visible = pending.pop()
        try:
            with os.scandir(base) as scan:
                raw = list(scan)
        except OSError:
            continue
        entries = []
        for entry in raw:
            is_dir = False
            with suppress(OSError):
                is_dir = entry.is_dir()
            entries.append((entry.name, is_dir, entry.is_symlink()))
        has_skill = any(name == "SKILL.md" and not directory for name, directory, _ in entries)
        yield base, entries, has_skill, skill_visible
        # Support subtrees contribute to neither index. Excluded dirs can still
        # contain legacy flat skills, including explicitly named symlink paths.
        for name, directory, _ in reversed(entries):
            if directory and not (has_skill and name in SKILL_SUPPORT_DIRS):
                pending.append((base / name, skill_visible and name not in EXCLUDED_SKILL_DIRS))


def _recursive_dirs(snapshot, root):
    """Path.rglob's '**' expansion over the saved tree, never following links."""
    pending = [root]
    if sys.version_info >= (3, 13):
        # pathlib's glob engine changed in 3.13: emit siblings before processing
        # the last sibling's subtree. Earlier versions used preorder DFS.
        yield root
        while pending:
            base = pending.pop()
            for name, directory, symlink in snapshot.tree.get(base, ()):
                if directory and not symlink:
                    child = base / name
                    yield child
                    pending.append(child)
    else:
        while pending:
            base = pending.pop()
            yield base
            pending.extend(base / name for name, directory, symlink in reversed(snapshot.tree.get(base, ()))
                           if directory and not symlink)


def invalidate_skill_index() -> None:
    """Fence snapshots built before any in-process skill mutation or explicit reload."""
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        _SNAPSHOTS.clear()


def _build_snapshot(root: Path) -> _Snapshot:
    from tools import skills_tool as st

    snapshot = _Snapshot(_GENERATION)
    for base, entries, has_skill, skill_visible in _walk_tree(root):
        snapshot.tree[base] = entries
        if has_skill and skill_visible:
            snapshot.skills.append(base / "SKILL.md")
        for filename, _, _ in entries:
            path = base / filename
            if (os.path.normcase(filename).endswith(".md") and filename != "SKILL.md"
                    and not st._is_skill_support_path(path)
                    and not st._is_package_owned_markdown(path, root)):
                snapshot.legacy.add(path)
    snapshot.skills.sort(key=str)  # iter_skill_index_files sorts strings, not Path components
    for index, path in enumerate(snapshot.skills):
        snapshot.by_directory.setdefault(path.parent.name, []).append(index)
        declared = st._safe_frontmatter(path).get("name")
        if isinstance(declared, str):
            snapshot.by_name.setdefault(declared, []).append(index)
    # Ordinary basename lookups are O(matches), in the original rglob order.
    for base in _recursive_dirs(snapshot, root):
        for filename, _, _ in snapshot.tree.get(base, ()):
            path = base / filename
            if path in snapshot.legacy:
                snapshot.flat.setdefault(os.path.normcase(filename), []).append(path)
    snapshot.built_at = time.monotonic()
    return snapshot


def _snapshot(root: Path, *, refresh: bool) -> _Snapshot:
    # Serialize cold builds too: concurrent first loads must not stampede the tree.
    # Invalidation uses the same lock, so it cannot publish a pre-write scan as fresh.
    with _LOCK:
        cached = _SNAPSHOTS.get(root)
        if (not refresh and cached is not None and cached.generation == _GENERATION
                and time.monotonic() - cached.built_at < _TTL_SECONDS):
            return cached
        built = _build_snapshot(root)
        if len(_SNAPSHOTS) >= _MAX_ROOTS and root not in _SNAPSHOTS:
            _SNAPSHOTS.pop(next(iter(_SNAPSHOTS)))
        _SNAPSHOTS[root] = built
        return built


def _select_pattern(snapshot, base, parts):
    """Relative glob components over saved entries; literal components may follow links."""
    if not parts:
        yield base
    elif parts[0] == "**":
        for directory in _recursive_dirs(snapshot, base):
            yield from _select_pattern(snapshot, directory, parts[1:])
    else:
        for name, directory, _ in snapshot.tree.get(base, ()):
            if fnmatch.fnmatch(name, parts[0]) and (directory or len(parts) == 1):
                yield from _select_pattern(snapshot, base / name, parts[1:])


def _legacy_matches(snapshot: _Snapshot, root: Path, name: str):
    pattern = f"{name}.md"
    if not any(c in pattern for c in "/\\*?["):
        yield from snapshot.flat.get(os.path.normcase(pattern), ())
        return
    # Match relative to THIS root, never against its absolute ancestor components.
    # '**' cannot follow links, but an explicitly named component can (rglob parity).
    parts = Path(pattern).parts
    if sys.version_info < (3, 13) and any("**" in part and part != "**" for part in parts):
        raise ValueError("Invalid pattern: '**' can only be an entire path component")
    seen = set()
    for path in _select_pattern(snapshot, root, ("**", *parts)):
        if path in snapshot.legacy and path not in seen:
            seen.add(path)
            yield path


def _recursive_support_path(path, root):
    parts = path.relative_to(root).parts
    return any(part in SKILL_SUPPORT_DIRS and (root.joinpath(*parts[:i]) / "SKILL.md").exists()
               for i, part in enumerate(parts[:-1]))


def _collect(name, local_category_name, all_dirs, *, refresh):
    from tools import skills_tool as st

    candidates, seen = [], set()
    stale = False

    def record(directory, path):
        key = path
        with suppress(Exception):
            key = path.resolve()
        if key not in seen:
            seen.add(key)
            candidates.append((directory, path))

    for root in all_dirs:
        snapshot = _snapshot(root, refresh=refresh)
        for direct in filter(None, (name, local_category_name)):
            path = root / direct
            flat = path.with_suffix(".md")
            if not st._is_skill_support_path(path) and path.is_dir() and (path / "SKILL.md").exists():
                record(path, path / "SKILL.md")
            elif (flat.exists() and not st._is_skill_support_path(flat)
                  and not st._is_package_owned_markdown(flat, root)):
                record(None, flat)
        indexes = sorted(set(snapshot.by_directory.get(name, ())) | set(snapshot.by_name.get(name, ())))
        for index in indexes:
            path = snapshot.skills[index]
            if (path.exists() and not _recursive_support_path(path, root)
                    and (path.parent.name == name or st._safe_frontmatter(path).get("name") == name)):
                record(path.parent, path)
            else:
                stale = True
        for path in _legacy_matches(snapshot, root, name):
            if (path.exists() and not st._is_skill_support_path(path)
                    and not st._is_package_owned_markdown(path, root)):
                record(None, path)
            else:
                stale = True
    return candidates, stale


def collect_skill_candidates(name, local_category_name, all_dirs):
    """Direct, recursive dir/frontmatter, then legacy candidates, in root order.

    Hits validate only their candidates. A miss or changed candidate refreshes all
    roots and retries once, so out-of-process creates/renames/deletes resolve now.
    A duplicate created by ANOTHER process during the TTL can remain unseen until
    expiry; in-process writes invalidate immediately. Tier/collision/quarantine
    decisions remain in locate_skill, never cached here.
    """
    candidates, stale = _collect(name, local_category_name, all_dirs, refresh=False)
    if stale or not candidates:
        candidates, _ = _collect(name, local_category_name, all_dirs, refresh=True)
    return candidates
