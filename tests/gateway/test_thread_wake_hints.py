"""Owner-lifetime hints with real isolated admissions and durable wake state.

Only notice transport is the existing deterministic owner double; no provider runs.
"""
import contextlib
import threading
import time
from types import SimpleNamespace

import pytest

from gateway import thread_wake as wake
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_contract import SessionRef
from gateway.session_settlement_recovery import commit_and_publish
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from tests.tools.test_thread_tool import DANNY, _db, call, thread
from tests.tools.test_thread_tool import owner as owner  # noqa: PLC0414 -- pytest fixture re-export
from tools import thread_state as ts

pytest_plugins = ['tests.tools.test_thread_tool']


@pytest.fixture(autouse=True)
def isolated_hints(monkeypatch):
    monkeypatch.setattr(wake, '_changed', threading.Event())


@pytest.fixture
def settlement(home):
    with _db(home) as db:
        thread(db, 'child')
        epoch = begin_runtime_epoch(db, instance_id='hint-fixture')
        runner = SimpleNamespace(_draining=False)
        authority = SessionAuthority(runner, profile_id=str(home), instance_id='hint-fixture', db=db, epoch=epoch)
        for sid in ('parent', 'child'):
            authority.sessions[sid] = LiveSession(
                SessionSource(platform=Platform.TELEGRAM, chat_id=sid, user_id=DANNY), sid)
        yield authority


def claimed(authority, sid):
    admit_session_input(authority.db, epoch=authority.epoch, principal_id=DANNY,
                        session_id=sid, request_id='thread-' + sid, payload={'text': 'work'})
    row = claim_session_input(authority.db, epoch=authority.epoch, session_id=sid)
    assert isinstance(row, dict)
    return row


def complete(authority, row):
    sid = row['target_session_id']
    commit_and_publish(authority, authority.sessions[sid], SessionRef(authority.profile_id, sid),
                       row, 'exact child answer', 'completed',
                       {'result': {'final_response': 'exact child answer', 'completed': True}}, {})


@contextlib.contextmanager
def running(home, monkeypatch, interval=60):
    sweeper = wake.Sweeper(lambda: [home])
    passed = threading.Event()
    original = sweeper.sweep
    passes = []

    def observed():
        value = original()
        passes.append(time.monotonic())
        passed.set()
        return value

    monkeypatch.setattr(sweeper, 'sweep', observed)
    stop = threading.Event()
    worker = threading.Thread(target=sweeper.run, args=(stop, interval), daemon=True)
    worker.start()
    try:
        yield sweeper, passed, passes
    finally:
        stop.set()
        if hasattr(wake, 'hint'):
            wake.hint()
        worker.join(timeout=2)
        # The baseline cannot wake its long periodic wait; daemon exits with the fixture process.
        if hasattr(wake, 'hint'):
            assert not worker.is_alive()


def test_committed_completion_wakes_existing_worker_once(home, owner, settlement, monkeypatch):
    row = claimed(settlement, 'child')
    owner.update_outcome = None
    with running(home, monkeypatch) as (_, passed, _):
        complete(settlement, row)
        assert passed.wait(3), 'durable child completion waited for the periodic sweep'
        assert len(owner.submits()) == 1
        assert 'exact child answer' in owner.submits()[0]['text']
        assert not ts.delivered(home, row['admission_id'], 'done')
        passed.clear()
        for _ in range(100):
            wake.hint()
        assert passed.wait(3)
        assert len(owner.submits()) == 1
        assert not ts.delivered(home, row['admission_id'], 'done')


@pytest.mark.parametrize('idle_path', ['resident', 'worker'])
def test_busy_lead_waits_then_owner_idle_prompts_delivery(home, owner, settlement, monkeypatch, idle_path):
    child, lead = claimed(settlement, 'child'), claimed(settlement, 'parent')
    with running(home, monkeypatch) as (_, passed, _):
        complete(settlement, child)
        assert passed.wait(3)
        assert owner.submits() == []
        assert not ts.delivered(home, child['admission_id'], 'done')
        passed.clear()
        if idle_path == 'resident':
            complete(settlement, lead)
            settlement._publish_pending(SessionRef(str(home), 'parent'))
        else:
            # Worker RPC commits before wake_after_worker; exercise that exact idle callback.
            from gateway.session_results import retain_result
            retain_result(settlement.db, epoch=settlement.epoch, row=lead,
                          result={'result': {'final_response': 'idle'}})
            monkeypatch.setattr(settlement, '_schedule', lambda ref: None)
            settlement.wake_after_worker('parent')
        assert passed.wait(3), 'owner idle transition waited for the periodic sweep'
        assert len(owner.submits()) == 1


def test_disabled_hint_recovers_periodically_and_restart_keeps_pending(home, owner, settlement, monkeypatch):
    row = claimed(settlement, 'child')
    owner.update_outcome = None
    monkeypatch.setattr(wake, 'hint', lambda: None)
    with running(home, monkeypatch, interval=0.05) as (_, passed, _):
        complete(settlement, row)
        assert passed.wait(3)
        assert len(owner.submits()) == 1
        assert not ts.delivered(home, row['admission_id'], 'done')
    from tests.tools.test_thread_tool import settle
    notice = settlement.db._read_one(
        "SELECT admission_id FROM session_admissions WHERE request_id LIKE 'thread_update:%'")
    assert notice is not None
    settle(settlement.db, notice['admission_id'], 'completed')
    with running(home, monkeypatch, interval=0.05) as (_, passed, _):
        assert passed.wait(3)
        assert ts.delivered(home, row['admission_id'], 'done')
        assert len(owner.submits()) == 1


def test_explicit_exact_read_while_busy_suppresses_later_hint(home, owner, settlement, monkeypatch):
    child, lead = claimed(settlement, 'child'), claimed(settlement, 'parent')
    with running(home, monkeypatch) as (_, passed, _):
        complete(settlement, child)
        assert passed.wait(3)
        result = call(action='read', thread_id='child', admission_id=child['admission_id'])
        assert result['result']['final_response'] == 'exact child answer'
        assert ts.delivered(home, child['admission_id'], 'done')
        passed.clear()
        complete(settlement, lead)
        assert passed.wait(3)
        assert owner.submits() == []


def test_hint_storm_is_coalesced_and_backoff_is_preserved(home, owner, settlement, monkeypatch):
    row = claimed(settlement, 'child')
    owner.refuse_submit = True
    with running(home, monkeypatch) as (sweeper, passed, passes):
        complete(settlement, row)
        assert passed.wait(3)
        retry = dict(sweeper._retry)
        assert len(owner.submits()) == 1
        passed.clear()
        for _ in range(1000):
            wake.hint()
        assert passed.wait(3)
        assert sweeper._retry == retry
        assert len(owner.submits()) == 1
        assert passes[1] - passes[0] >= wake.HINT_MIN_INTERVAL_S


def test_hint_never_scans_unserved_profiles(home, owner, settlement, monkeypatch):
    row = claimed(settlement, 'child')
    sweeper = wake.Sweeper(list)
    stop, passed = threading.Event(), threading.Event()
    original = sweeper.sweep

    def observed():
        original()
        passed.set()

    monkeypatch.setattr(sweeper, 'sweep', observed)
    worker = threading.Thread(target=sweeper.run, args=(stop, 60), daemon=True)
    worker.start()
    try:
        complete(settlement, row)
        assert passed.wait(3)
        assert owner.submits() == []
    finally:
        stop.set()
        if hasattr(wake, 'hint'):
            wake.hint()
        worker.join(timeout=2)


def test_stale_epoch_cannot_commit_or_signal_completion(home, owner, settlement, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    row = claimed(settlement, 'child')
    begin_runtime_epoch(settlement.db, instance_id='replacement')
    signals = []
    monkeypatch.setattr(wake, 'hint', lambda: signals.append(True))
    with pytest.raises(RuntimeStoreError):
        complete(settlement, row)
    assert signals == []
    assert owner.submits() == []


def test_shutdown_wakes_and_joins_the_owner_lifetime_worker(home, monkeypatch):
    from gateway import run
    monkeypatch.setattr(run, '_handoff_watch_scopes', lambda runner: [])
    workers = []
    original = wake.start

    def start(*args):
        worker = original(*args)
        workers.append(worker)
        return worker

    monkeypatch.setattr(wake, 'start', start)
    finish = wake.start_for(SimpleNamespace())
    finish()
    assert len(workers) == 1
    assert not workers[0].is_alive()
    finish()  # repeated lifecycle cleanup is harmless


def test_unknown_lead_remains_a_barrier_despite_hints(home, owner, settlement, monkeypatch):
    from gateway.session_settlement_recovery import _mark_unknown
    child, lead = claimed(settlement, 'child'), claimed(settlement, 'parent')
    assert _mark_unknown(settlement, lead) == 'unknown'
    with running(home, monkeypatch) as (_, passed, _):
        complete(settlement, child)
        assert passed.wait(3)
        assert owner.submits() == []
        assert not ts.delivered(home, child['admission_id'], 'done')


@pytest.mark.platforms('linux')
def test_managed_worker_result_uses_the_same_committed_completion_hint(home, owner, settlement, monkeypatch):
    import asyncio
    import subprocess
    import sys
    from gateway import session_managed_worker as managed
    from gateway.session_policy import build_policy
    from tests.gateway.test_managed_worker_finished_exit import CHILD

    processes = []

    class Child(subprocess.Popen):
        def __init__(self, args, **kwargs):
            super().__init__([sys.executable, '-c', CHILD, 'nonzero'], **kwargs)
            processes.append(self)

    monkeypatch.setattr(managed.subprocess, 'Popen', Child)
    settlement.runner._cached_agent_for = lambda route: None
    settlement.runner._adapter_for_source = lambda source: None
    row = claimed(settlement, 'child')
    ref = SessionRef(str(home), 'child')
    live = settlement.sessions['child']
    settlement._stamp_claim(ref, live, row)
    policy = build_policy({'cwd': str(home), 'model': 'm'}, {}, profile_terminal=False)

    async def execute():
        async with asyncio.timeout(40):
            return await managed.execute_managed(settlement, ref, row, policy)

    response = asyncio.run(execute())
    assert response == 'FINISHED_ANSWER'
    assert all(process.poll() is not None for process in processes)
    stored = settlement.db._read_one('SELECT status FROM session_admissions WHERE admission_id=?',
                                     (row['admission_id'],))
    assert stored is not None and stored['status'] == 'started'
    with running(home, monkeypatch) as (_, passed, _):
        commit_and_publish(settlement, live, ref, row, response, 'completed',
                           settlement.pending_results[row['admission_id']], {})
        assert passed.wait(3)
        assert len(owner.submits()) == 1
        assert 'FINISHED_ANSWER' in owner.submits()[0]['text']
