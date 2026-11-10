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


@pytest.mark.parametrize('linked_worktree', [False, True])
def test_route_reuses_status_with_ttl_and_git_stamp_invalidation(repo, monkeypatch, linked_worktree):
    import asyncio
    from hermes_cli.web_routers import git as router
    if linked_worktree:
        target = repo.parent / 'linked'
        _git(repo, 'worktree', 'add', '-b', 'linked-status', str(target))
        (target / 'a.txt').write_text('one\ntwo\nthree\n')
        (target / 'new.py').write_text('print(1)\nprint(2)\n')
        repo = target
    now, calls = [1000.0], []
    monkeypatch.setattr(router.time, 'monotonic', lambda: now[0])
    original = web_git.repo_status
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(web_git, 'repo_status', counted)
    def status():
        return asyncio.run(router.git_status_route(str(repo)))
    first = status()
    first['changed'] = -1
    assert status()['changed'] == 2
    assert len(calls) == 1
    _git(repo, 'add', 'a.txt')
    assert status()['staged'] == 1
    assert len(calls) == 2
    _git(repo, 'checkout', '-b', 'status-cache-branch')
    status()
    assert len(calls) == 3
    now[0] += 5
    status()
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_route_coalesces_status_and_a_cancelled_waiter_does_not_cancel_probe(repo, monkeypatch):
    import asyncio
    import threading
    from hermes_cli.web_routers import git as router
    from tests.waiting import async_wait_until
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = web_git.repo_status
    def blocked(path):
        calls.append(path)
        entered.set()
        assert release.wait(10)
        return original(path)
    monkeypatch.setattr(web_git, 'repo_status', blocked)
    first = asyncio.create_task(router.git_status_route(str(repo)))
    await async_wait_until(entered.is_set, timeout=5)
    second = asyncio.create_task(router.git_status_route(str(repo)))
    await asyncio.sleep(0)
    first.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    assert (await second)['changed'] == 2
    assert len(calls) == 1
