"""A traversal error must not erase the file matches ripgrep already found."""

import json
import os

import pytest

from tools import file_tools
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.registry import registry

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture(scope="module")
def local_env(tmp_path_factory):
    return LocalEnvironment(cwd=str(tmp_path_factory.mktemp("partial-file-search")))


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read mode-000 directories")
@pytest.mark.parametrize("native", ["0", "1"])
@pytest.mark.parametrize("order", ["discovery", "modified"])
@pytest.mark.parametrize("multi_root", [False, True])
@pytest.mark.parametrize("readable_matches", [False, True])
def test_unreadable_subtree_preserves_matches_or_fails_clearly(
        local_env, tmp_path, monkeypatch, native, order, multi_root, readable_matches):
    ops = ShellFileOperations(local_env, cwd=str(tmp_path))
    if not ops._has_command("rg"):
        pytest.skip("ripgrep is not installed")
    if order == "modified" and ops._modified_rg_capability_error(ops._resolve_command("rg")):
        pytest.skip("exact modified ordering requires ripgrep 14+")
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    root = tmp_path / "tree"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    readable = root / "readable result.txt"
    if readable_matches:
        readable.write_text("accessible\n", encoding="utf-8")
    locked = root / "locked"
    locked.mkdir()
    (locked / "unreadable.txt").write_text("inaccessible\n", encoding="utf-8")
    locked.chmod(0)
    try:
        result = json.loads(registry.dispatch("search_files", {
            "pattern": "*.txt", "target": "files", "order": order,
            "path": f"{root}, {other}" if multi_root else str(root),
        }, task_id=str(tmp_path)))
    finally:
        locked.chmod(0o700)

    if not readable_matches:
        assert result.get("error"), result
        assert not result.get("files"), result
        assert result["total_count"] == 0
        return
    assert not result.get("error"), result
    assert result["files"] == [str(readable)]
    assert result["total_count"] == 1
    assert result["total_count_is_lower_bound"] is True
    assert result["truncated"] is True
    assert result["limit_reason"] == "search_error"
    warning = result["warning"].lower()
    assert "partial" in warning and "ripgrep" in warning and "unreadable" in warning
    if multi_root:
        assert "searched 2" in warning


def test_native_launch_failure_is_not_a_filename(local_env, tmp_path, monkeypatch):
    ops = ShellFileOperations(local_env, cwd=str(tmp_path))
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "1")
    result = ops._search_files_rg("*.txt", str(tmp_path), 50, 0,
                                  rg_executable=str(tmp_path / "missing-rg"))
    assert result.error
    assert not result.files
    assert result.total_count == 0


def test_partial_warning_survives_exclusion_notice(local_env, tmp_path, monkeypatch):
    from tools.file_operations_common import SearchResult

    ops = ShellFileOperations(local_env, cwd=str(tmp_path))
    partial = SearchResult(files=[str(tmp_path / "readable.txt")], warning="Partial search: unreadable paths.")
    monkeypatch.setattr(ops, "_dispatch_search", lambda *args: partial)
    monkeypatch.setattr(ops, "_macos_search_exclusions", lambda path: ["Documents"])

    result = ops.search("*.txt", path=str(tmp_path), target="files")

    assert result.warning
    assert "unreadable paths" in result.warning
    assert "Skipped macOS protected folders" in result.warning


@pytest.mark.parametrize("merge_stderr", [False, True])
def test_native_interrupt_diagnostic_respects_output_stream(local_env, tmp_path, monkeypatch, merge_stderr):
    from tools import interrupt

    ops = ShellFileOperations(local_env, cwd=str(tmp_path))
    monkeypatch.setattr(interrupt, "is_interrupted", lambda: True)

    result = ops._run_rg_native(["sh", "-c", "'sleep 30'"], 5, timeout=5, merge_stderr=merge_stderr)

    assert result.exit_code == 130
    assert result.stdout == ("\n[Command interrupted]" if merge_stderr else "")


_SHELL_NOTICES = [
    ("/repo/a.py\n[Command interrupted]", 130),
    ("[Command interrupted - Modal sandbox exec cancelled]", 130),
    ("/repo/a.py\n/repo/b.py", 137),
]


@pytest.mark.parametrize("stdout,exit_code", _SHELL_NOTICES)
def test_shell_transport_failure_is_never_partial_file_results(local_env, tmp_path, monkeypatch, stdout, exit_code):
    """Shell transports append notices to stdout; only rg's own exit 2 is partial."""
    from tools.file_operations_common import ExecuteResult

    ops = ShellFileOperations(local_env, cwd=str(tmp_path))
    monkeypatch.setattr(ops, "_run_rg_bounded", lambda *a, **k: ExecuteResult(stdout=stdout, exit_code=exit_code))
    result = ops._search_files_rg("*.py", "/repo", 50, 0, rg_executable="rg")
    assert result.error
    assert not result.files
    assert not any("[Command" in f for f in (result.files or []))


@pytest.mark.parametrize("output_mode", ["files_only", "content", "count"])
def test_interrupted_content_search_is_an_error_not_a_result(output_mode):
    from tools.file_operations_common import ExecuteResult
    from tools.file_operations_search import _parse_search_output

    result = _parse_search_output(
        ExecuteResult(stdout="/repo/a.py:1:hit\n[Command interrupted]", exit_code=130), output_mode, 50, 0, 0)
    assert result.error and "interrupted" in result.error.lower()
    assert not result.files and not result.matches and not result.counts
