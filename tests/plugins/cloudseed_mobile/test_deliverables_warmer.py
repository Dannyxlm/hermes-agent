"""Files index warmer: converges without a Files open (round 9 U29, R24). Synthetic stores only."""
import sqlite3
import threading
import time

import pytest

from plugins.cloudseed_mobile import deliverables_index as ix
from plugins.cloudseed_mobile.deliverables_warmer import BACKOFF, IDLE, WORKING, DeliverablesWarmer
from tests.plugins.cloudseed_mobile.test_deliverables_index import store
from tests.plugins.cloudseed_mobile.test_workspace_files import setup_root
from tests.plugins.test_cloudseed_mobile import client  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _paths_exist(monkeypatch):
    monkeypatch.setattr(ix.DeliverablesIndex, '_exists', lambda self, row: True)


@pytest.fixture
def home_index(client):  # noqa: F811
    c, home = client
    root, wid = setup_root(c, home)
    store(home, root, 5)
    grants = [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}]
    return home, root, grants


def converge(warmer, limit=50):
    states = []
    for _ in range(limit):
        states.append(warmer.run_once())
        if states[-1] == IDLE:
            return states
    raise AssertionError(f'warmer did not converge: {states[-5:]}')


def test_version_bump_converges_without_a_files_request(home_index):
    home, _root, grants = home_index
    index = ix.DeliverablesIndex(home, 'default', grants)
    warmer = DeliverablesWarmer(lambda: [index], max_sessions=2)

    states = converge(warmer)
    assert WORKING in states
    snap = index.snapshot()
    assert snap['refresh_status'] == 'ready' and snap['indexed_sessions'] == 5
    assert not snap['warm_pending']

    with index.connection() as db:  # an older runtime's cache, as after a scan_version bump
        db.execute("UPDATE meta SET value='4' WHERE key='scan_version'")
    assert index.snapshot()['warm_pending']
    converge(warmer)
    snap = index.snapshot()
    assert snap['refresh_status'] == 'ready' and snap['indexed_sessions'] == 5


def test_source_churn_alone_is_left_to_files_opens(home_index):
    home, root, grants = home_index
    index = ix.DeliverablesIndex(home, 'default', grants)
    warmer = DeliverablesWarmer(lambda: [index], max_sessions=10)
    converge(warmer)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("INSERT INTO messages VALUES(99,'0','assistant','more',NULL,NULL,NULL,NULL,1,'99')")
    snap = index.snapshot()
    assert snap['refresh_pending'] and not snap['warm_pending']
    calls = []
    index.refresh = lambda **kw: calls.append(kw)
    assert warmer.run_once() == IDLE and calls == []


def test_grant_change_requeues_the_profile(home_index):
    home, root, grants = home_index
    current = {'grants': grants}
    warmer = DeliverablesWarmer(lambda: [ix.DeliverablesIndex(home, 'default', current['grants'])], max_sessions=10)
    converge(warmer)
    current['grants'] = grants + [{'id': 'other', 'root_path': str(root / 'outputs'), 'name': 'Other'}]
    assert ix.DeliverablesIndex(home, 'default', current['grants']).snapshot()['warm_pending']
    converge(warmer)
    assert not ix.DeliverablesIndex(home, 'default', current['grants']).snapshot()['warm_pending']


def test_lock_contention_backs_off_and_doubles_the_wait(home_index):
    home, _root, grants = home_index
    index = ix.DeliverablesIndex(home, 'default', grants)
    calls, waits = [], []
    index.refresh = lambda **kw: calls.append(kw)

    def wait(seconds):
        waits.append(seconds)
        return len(waits) > 4

    warmer = DeliverablesWarmer(lambda: [index], initial_delay=0, interval=10, max_backoff=35, wait=wait)
    assert index.lock.acquire(blocking=False)
    try:
        assert warmer.run_once() == BACKOFF
        warmer._run()
    finally:
        index.lock.release()
    assert calls == []
    assert waits == [0, 10, 20, 35, 35]


def test_failed_scan_resumes_from_its_checkpoint(home_index):
    home, _root, grants = home_index
    index = ix.DeliverablesIndex(home, 'default', grants)
    real_refresh = index.refresh
    calls, at_failure = [], []

    def flaky_refresh(**kw):
        calls.append(kw)
        if len(calls) == 2:
            at_failure.append(index.snapshot()['indexed_sessions'])
            raise OSError('disk hiccup')
        return real_refresh(**kw)

    index.refresh = flaky_refresh
    waits = []

    def wait(seconds):
        waits.append(seconds)
        return not index.snapshot()['warm_pending'] or len(waits) > 20

    DeliverablesWarmer(lambda: [index], initial_delay=0, interval=1, max_sessions=2, wait=wait)._run()
    snap = index.snapshot()
    assert at_failure == [2]
    assert snap['indexed_sessions'] == 5 and snap['refresh_status'] == 'ready'


def test_shutdown_cancels_the_inflight_refresh_and_joins():
    entered, cancelled = threading.Event(), []

    class SlowIndex:
        lock = threading.Lock()

        def snapshot(self):
            return {'warm_pending': True}

        def refresh(self, max_sessions, cancel):
            entered.set()
            cancelled.append(cancel.wait(10))

    warmer = DeliverablesWarmer(lambda: [SlowIndex()], initial_delay=0)
    warmer.start()
    assert entered.wait(5)
    started = time.monotonic()
    warmer.stop(timeout=5)
    assert time.monotonic() - started < 2
    assert cancelled == [True]
    assert warmer._thread is None
    assert not any(t.name == 'cloudseed-deliverables-warmer' for t in threading.enumerate())


def test_dashboard_lifespan_starts_and_stops_the_warmer(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.cloudseed_mobile.dashboard import plugin_api as api

    events = []

    class Recorder:
        def __init__(self, indexes, **_kw):
            events.append('init')

        def start(self):
            events.append('start')

        def stop(self, timeout=5.0):
            events.append('stop')

    monkeypatch.setattr(api, 'DeliverablesWarmer', Recorder)
    app = FastAPI()
    app.include_router(api.router, prefix='/api/plugins/cloudseed_mobile')
    with TestClient(app):
        assert events == ['init', 'start']
    assert events == ['init', 'start', 'stop']
