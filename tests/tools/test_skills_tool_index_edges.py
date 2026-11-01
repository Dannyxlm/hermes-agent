"""Regression contracts for snapshot refreshes and legacy lookup edge cases."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from tools import skills_tool as st
from tools import skills_tool_index as index


def _skill(root, directory, name):
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Test fixture.\n---\nInstructions.\n")
    return path


@pytest.fixture(autouse=True)
def clear_index():
    st.clear_skills_cache()
    yield
    st.clear_skills_cache()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["legacy", "skill"])
def test_dangling_sibling_does_not_rebuild_a_valid_hit(tmp_path, monkeypatch, kind):
    root = tmp_path / "skills"
    target = _skill(root, "live/shared", "shared")
    dead = root / ("dead/shared.md" if kind == "legacy" else "dead/shared/SKILL.md")
    dead.parent.mkdir(parents=True)
    destination = tmp_path / "missing.md"
    dead.symlink_to(destination)
    builds = []
    build = index._build_snapshot

    def counted(root):
        builds.append(root)
        return build(root)

    monkeypatch.setattr(index, "_build_snapshot", counted)
    expected = [(target.parent, target)]
    assert st._collect_skill_candidates("shared", None, [root]) == expected
    builds.clear()
    for _ in range(3):
        assert st._collect_skill_candidates("shared", None, [root]) == expected
    assert builds == [], "a lexically present dangling sibling is not a stale snapshot"
    destination.write_text("---\nname: shared\n---\nNow live.\n")
    candidates = st._collect_skill_candidates("shared", None, [root])
    assert {p for _, p in candidates} == {target, dead}


@pytest.mark.parametrize("create", [False, True])
def test_concurrent_misses_share_one_forced_build_per_root(tmp_path, monkeypatch, create):
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        _skill(root, "seed", "seed")
    assert len(st._collect_skill_candidates("seed", None, roots)) == 2
    builds = []
    build = index._build_snapshot
    collect = index._collect
    target = roots[1] / "nested/package/SKILL.md"

    def publish():
        if create:
            _skill(roots[1], "nested/package", "new-name")

    # Everyone must observe the same old snapshots before any refresh can start.
    ready = Barrier(12, action=publish, timeout=10)

    def first_pass(*args, **kwargs):
        result = collect(*args, **kwargs)
        if not kwargs["refresh"]:
            ready.wait()
        return result

    def counted(root):
        builds.append(root)
        return build(root)

    monkeypatch.setattr(index, "_collect", first_pass)
    monkeypatch.setattr(index, "_build_snapshot", counted)
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(st._collect_skill_candidates, "new-name", None, roots) for _ in range(12)]
        expected = [(target.parent, target)] if create else []
        assert all(f.result(timeout=15) == expected for f in futures)
    assert {root: builds.count(root) for root in roots} == {root: 1 for root in roots}


def _resident_slots(value):
    """Count retained container entries without depending on snapshot field names."""
    if isinstance(value, dict):
        return len(value) + sum(_resident_slots(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return len(value) + sum(_resident_slots(v) for v in value)
    return 0


def test_excluded_payload_is_not_retained_but_legacy_matches_keep_order(tmp_path):
    root = tmp_path / "skills"
    _skill(root, "seed", "seed")
    legacy = root / "node_modules/vendor/deep/shared.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("Excluded-tree legacy skill.")
    (root / "visible").mkdir()
    (root / "visible/shared.md").write_text("Visible legacy skill.")
    expected = [(None, path) for path in root.rglob("shared.md")]
    assert st._collect_skill_candidates("shared", None, [root]) == expected
    before = _resident_slots(vars(index._SNAPSHOTS[root]))
    for n in range(40):
        payload = root / "node_modules" / f"dependency-{n}/lib/payload.js"
        payload.parent.mkdir(parents=True)
        payload.write_text("Unrelated dependency payload.")
    st.clear_skills_cache()
    assert st._collect_skill_candidates("shared", None, [root]) == expected
    after = _resident_slots(vars(index._SNAPSHOTS[root]))
    assert after == before, "excluded non-Markdown payload must not grow resident snapshot entries"
    for name in ("sh*", "node_modules/vendor/**/shared", "**/shared"):
        assert st._collect_skill_candidates(name, None, [root]) == [(None, p) for p in root.rglob(f"{name}.md")]


def test_complex_legacy_support_matches_do_not_force_snapshot_refresh(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    owned = _skill(root, "package", "package").parent / "references/shared.md"
    owned.parent.mkdir()
    owned.write_text("Support content.")
    legacy = root / "node_modules/shared.md"
    legacy.parent.mkdir()
    legacy.write_text("Legacy skill.")
    assert st._collect_skill_candidates("sh*", None, [root]) == [(None, legacy)]
    builds = []
    build = index._build_snapshot

    def counted(root):
        builds.append(root)
        return build(root)

    monkeypatch.setattr(index, "_build_snapshot", counted)
    assert st._collect_skill_candidates("sh*", None, [root]) == [(None, legacy)]
    assert builds == []


def _uncached_candidates(name, category, roots):
    """Pre-index collector contract; dangling targets are deliberately omitted."""
    from agent.skill_utils import iter_skill_index_files

    candidates, seen = [], set()

    def record(directory, path):
        if path.exists() and path.resolve() not in seen:
            seen.add(path.resolve())
            candidates.append((directory, path))

    for root in roots:
        for direct in filter(None, (name, category)):
            path = root / direct
            flat = path.with_suffix(".md")
            if not st._is_skill_support_path(path) and path.is_dir() and (path / "SKILL.md").exists():
                record(path, path / "SKILL.md")
            elif flat.exists() and not st._is_skill_support_path(flat) and not st._is_package_owned_markdown(flat, root):
                record(None, flat)
        for path in iter_skill_index_files(root, "SKILL.md"):
            if path.parent.name == name or st._safe_frontmatter(path).get("name") == name:
                record(path.parent, path)
        for path in root.rglob(f"{name}.md"):
            if path.name != "SKILL.md" and not st._is_skill_support_path(path) and not st._is_package_owned_markdown(path, root):
                record(None, path)
    return candidates


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("reverse", [False, True])
def test_two_root_differential_candidates_cold_and_warm(tmp_path, reverse):
    roots = [tmp_path / "first", tmp_path / "second"]
    linked = tmp_path / "external"
    _skill(linked, "package", "linked-alias")
    (linked / "shared.md").write_text("Linked flat.")
    for root in roots:
        directories = ["shared", "a/pkg", "a-b/pkg", "b/shared", ".archive/hidden", "node_modules/pkg"]
        for directory in reversed(directories) if reverse else directories:
            _skill(root, directory, "shared")
        for relative in ("shared.md", "a/shared.md", "a/deep/shared.md", "b/shared.md",
                         ".archive/deep/shared.md", "node_modules/shared.md", "references/shared.md",
                         "shared/references/shared.md", "shared/extra.md", "a[1].md", "a?b.md"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Legacy or support content.")
        (root / "directory.md").mkdir()
        (root / "dead.md").symlink_to(tmp_path / "absent.md")
        (root / "link").symlink_to(linked, target_is_directory=True)
        (root / ".archive/link").symlink_to(linked, target_is_directory=True)
        (root / "alias.md").symlink_to(root / "shared.md")
        dead_skill = root / "dangling/SKILL.md"
        dead_skill.parent.mkdir()
        dead_skill.symlink_to(tmp_path / "absent-skill.md")
    if reverse:
        roots.reverse()
    queries: list[tuple[str, str | None]] = [(name, None) for name in (
        "shared", "pkg", "hidden", "linked-alias", "shared/extra", "references/shared",
        "directory", "alias", "dead", "dangling", "SKILL", "a[1]", "a?b", "*", "sh*",
        "**/shared", "a/**/shared", ".archive/deep/shared", "node_modules/shared",
        "link/shared", ".archive/link/shared", "link/package", "a/de**", "absent")]
    queries.append(("unknown:shared", "a/pkg"))
    for name, category in queries:
        st.clear_skills_cache()
        try:
            expected = _uncached_candidates(name, category, roots)
        except ValueError:  # pathlib before 3.13 rejects embedded '**'.
            with pytest.raises(ValueError):
                st._collect_skill_candidates(name, category, roots)
        else:
            for _ in range(2):
                assert st._collect_skill_candidates(name, category, roots) == expected, (name, category)


def test_invalidation_fences_an_overlapping_build(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    old = _skill(root, "old", "shared")
    built, release, invalidating = Event(), Event(), Event()
    build = index._build_snapshot

    def paused(root):
        snapshot = build(root)
        built.set()
        assert release.wait(10)
        return snapshot

    def invalidate():
        invalidating.set()
        st.clear_skills_cache()

    monkeypatch.setattr(index, "_build_snapshot", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(st._collect_skill_candidates, "shared", None, [root])
        try:
            assert built.wait(10)
            new = _skill(root, "new", "shared")
            writer = pool.submit(invalidate)
            assert invalidating.wait(10)
        finally:
            release.set()
        reader.result(timeout=15)
        writer.result(timeout=15)
    monkeypatch.setattr(index, "_build_snapshot", build)
    assert {p for _, p in st._collect_skill_candidates("shared", None, [root])} == {old, new}
