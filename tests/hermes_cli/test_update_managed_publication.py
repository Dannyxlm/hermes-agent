"""A paired Desktop publication admits only its exact commit, without history repair."""
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main as cli_main, update_cmd
from tests.hermes_cli.test_update_target_identity import git, update_tree  # noqa: F401


@pytest.mark.parametrize("case", ["behind", "current", "ahead", "diverged", "moved", "invalid", "branch", "merge-noop"])
def test_managed_publication_uses_exact_git_identity(update_tree, monkeypatch, capsys, case):
    t = update_tree
    git(t.clone, "checkout", "-q", "main")
    t.args.channel = "main"
    target = t.newer
    if case in {"current", "ahead"}:
        git(t.clone, "fetch", "origin", "main")
        git(t.clone, "merge", "--ff-only", target)
    if case in {"ahead", "diverged"}:
        (t.clone / "local.txt").write_text("keep local history\n", encoding="utf-8")
        git(t.clone, "add", "local.txt")
        git(t.clone, "-c", "commit.gpgsign=false", "commit", "-qm", "local history")
    before = git(t.clone, "rev-parse", "HEAD")
    monkeypatch.setenv("HERMES_MANAGED_PUBLICATION_UPDATE", "1")
    monkeypatch.setenv("HERMES_MANAGED_PUBLICATION_REPOSITORY", "Dannyxlm/hermes-agent")
    monkeypatch.setenv("HERMES_MANAGED_PUBLICATION_BRANCH", "other" if case == "branch" else "main")
    monkeypatch.setenv("HERMES_MANAGED_PUBLICATION_TARGET_SHA",
                       "invalid" if case == "invalid" else t.wanted if case == "moved" else target)
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ())
    run = subprocess.run
    merges = []

    def observe(command, *args, **kwargs):
        assert not ("reset" in command and "--hard" in command), "managed update reset local history"
        if "merge" in command and "--ff-only" in command:
            merges.append(command[-1])
            if case == "merge-noop":
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    if case in {"behind", "current"}:
        cli_main.cmd_update(t.args)
        request, = t.requests
        assert request["expected_sha"] == target
        assert git(t.clone, "rev-parse", "HEAD") == target
        assert merges == ([target] if case == "behind" else [])
    else:
        with pytest.raises(SystemExit) as error:
            cli_main.cmd_update(t.args)
        assert error.value.code == 1
        assert t.requests == []
        assert git(t.clone, "rev-parse", "HEAD") == before
        assert "Managed Desktop update stopped" in capsys.readouterr().out
        assert merges == ([target] if case in {"diverged", "merge-noop"} else [])
        if case in {"ahead", "diverged"}:
            assert (t.clone / "local.txt").read_text(encoding="utf-8") == "keep local history\n"


def test_managed_publication_refuses_zip_before_download(tmp_path, monkeypatch, capsys):
    from hermes_cli import update_cmd_zip

    root = tmp_path / "release"
    root.mkdir()
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    monkeypatch.setenv("HERMES_MANAGED_PUBLICATION_UPDATE", "1")
    monkeypatch.setattr(update_cmd_zip, "_download_and_swap_zip",
                        lambda *_: pytest.fail("managed publication attempted an unpinned archive"))
    with pytest.raises(SystemExit) as error:
        update_cmd_zip._update_via_zip(SimpleNamespace(branch="main"))
    assert error.value.code == 1
    assert "not release-pinned" in capsys.readouterr().out
    assert list(root.iterdir()) == []
