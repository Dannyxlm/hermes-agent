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
def test_route_reads_mutations_immediately_after_completed_flight(repo, monkeypatch, linked_worktree):
    import asyncio
    from hermes_cli.web_routers import git as router
    if linked_worktree:
        target = repo.parent / 'linked'
        _git(repo, 'worktree', 'add', '-b', 'linked-status', str(target))
        repo = target
    else:
        _git(repo, 'checkout', '--', 'a.txt')
        (repo / 'new.py').unlink()
    calls = []
    original = web_git.repo_status
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(web_git, 'repo_status', counted)
    async def probe():
        first = await router.git_status_route(str(repo))
        assert first['changed'] == 0
        first['changed'] = -1
        (repo / 'a.txt').write_text('one\ntwo\nthree\n')
        (repo / 'new.py').write_text('print(1)\nprint(2)\n')
        changed = await router.git_status_route(str(repo))
        assert (changed['changed'], changed['unstaged'], changed['untracked']) == (2, 2, 1)
        assert len(calls) == 2
        web_git.review_revert(str(repo), 'new.py')
        assert (await router.git_status_route(str(repo)))['untracked'] == 0
        assert len(calls) == 3
    asyncio.run(probe())


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


def test_route_malformed_git_file_keeps_null_non_repo_response(tmp_path):
    import asyncio
    from hermes_cli.web_routers import git as router
    (tmp_path / '.git').write_bytes(b'gitdir: \xff\xfe')
    assert asyncio.run(router.git_status_route(str(tmp_path))) is None


@pytest.mark.asyncio
async def test_abandoned_failed_flight_is_observed_and_next_request_retries(repo, monkeypatch):
    import asyncio
    import gc
    from hermes_cli.web_routers import git as router
    entered, release, failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    errors, calls = [], []
    async def probe(*args):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            failed.set()
            raise RuntimeError('fixture git failure')
        return {'changed': 2}
    monkeypatch.setattr(router, '_git_op', probe)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        waiter = asyncio.create_task(router.git_status_route(str(repo)))
        await asyncio.wait_for(entered.wait(), timeout=5)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        del waiter
        release.set()
        await asyncio.wait_for(failed.wait(), timeout=5)
        await asyncio.sleep(0)  # Let completion callbacks consume the now-unobserved exception.
        gc.collect()
        # Python 3.14 shield deliberately logs a late exception even when it was retrieved.
        assert all(error['message'] == 'RuntimeError exception in shielded future' for error in errors)
        assert (await router.git_status_route(str(repo)))['changed'] == 2
        assert len(calls) == 2
    finally:
        release.set()
        loop.set_exception_handler(previous)
