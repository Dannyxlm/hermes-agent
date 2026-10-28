"""Option-shaped regexes are data in every content-search backend."""

import json

import pytest

from tools import file_tools
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.registry import registry

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture(scope="module")
def local_env(tmp_path_factory):
    return LocalEnvironment(cwd=str(tmp_path_factory.mktemp("option-patterns")))


@pytest.fixture(params=["rg-native", "rg-shell", "grep", "grep-pruned"])
def search(request, local_env, tmp_path, monkeypatch):
    backend = request.param
    ops = ShellFileOperations(local_env, cwd=str(tmp_path))
    engine = "rg" if backend.startswith("rg-") else "grep"
    if not ops._has_command(engine):
        pytest.skip(f"{engine} is not installed")
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "1" if backend == "rg-native" else "0")
    if engine == "grep":
        resolve = ops._resolve_command
        monkeypatch.setattr(ops, "_resolve_command", lambda cmd: None if cmd == "rg" else resolve(cmd))
        # Exercise both grep routes even when pytest's own temp root is hidden.
        monkeypatch.setattr(ops, "_root_under_hidden_dir", lambda path: backend == "grep-pruned")
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)

    def dispatch(pattern, **kwargs):
        return json.loads(registry.dispatch(
            "search_files", {"pattern": pattern, "path": str(tmp_path), **kwargs},
            task_id=str(tmp_path)))

    return dispatch


@pytest.mark.parametrize("output_mode", ["content", "files_only", "count"])
def test_leading_hyphen_regex_matches_as_data(search, tmp_path, output_mode):
    path = tmp_path / "flags.txt"
    path.write_text("before\n--not-a-real-flag\nafter\n", encoding="utf-8")

    result = search("--not-a-real-flag", file_glob="*.txt", output_mode=output_mode)

    assert not result.get("error"), result
    assert result["total_count"] == 1
    if output_mode == "content":
        assert result["matches"] == [{"path": str(path), "line": 2, "content": "--not-a-real-flag"}]
    elif output_mode == "files_only":
        assert result["files"] == [str(path)]
    else:
        assert result["counts"] == {str(path): 1}


@pytest.mark.parametrize("native", ["0", "1"])
@pytest.mark.parametrize("pattern,text,filename,hint", [
    ("--NEEDLE", "--needle", "case.txt", "case-insensitive"),
    ("--needle", "--needle", ".hidden.txt", "hidden or gitignored"),
    ("--needle[0]", "--needle[0]", "literal.txt", "literal match"),
])
def test_zero_match_probes_accept_leading_hyphens(local_env, tmp_path, monkeypatch,
                                                native, pattern, text, filename, hint):
    ops = ShellFileOperations(local_env, cwd=str(tmp_path))
    if not ops._has_command("rg"):
        pytest.skip("ripgrep is not installed")
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", native)
    (tmp_path / filename).write_text(text + "\n", encoding="utf-8")

    warning = ops._zero_match_probe(pattern, str(tmp_path), None)

    assert hint in (warning or "")
