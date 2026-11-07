"""Status does not launch a diff when porcelain proves no tracked paths changed."""
import pytest

from hermes_cli import web_git
from tests.hermes_cli.test_web_server_git import _git

pytest_plugins = ["tests.hermes_cli.test_web_server_git"]


@pytest.mark.parametrize('untracked', [False, True])
def test_status_skips_numstat_without_tracked_changes(repo, monkeypatch, untracked):
    _git(repo, 'checkout', '--', 'a.txt')
    if not untracked:
        (repo / 'new.py').unlink()

    def forbidden(*args):
        raise AssertionError('status launched an unnecessary tracked diff')

    monkeypatch.setattr(web_git, '_numstat', forbidden)
    result = web_git.repo_status(str(repo))
    assert result['changed'] == int(untracked)
    assert result['added'] == (2 if untracked else 0)
    assert result['removed'] == 0


def test_status_still_counts_tracked_and_untracked_changes(repo):
    result = web_git.repo_status(str(repo))
    assert (result['changed'], result['added'], result['removed']) == (2, 3, 0)
    _git(repo, 'add', 'a.txt')
    staged = web_git.repo_status(str(repo))
    assert (staged['staged'], staged['added']) == (1, 3)
