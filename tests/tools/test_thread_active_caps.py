"""Working-thread caps cover creates and reactivation, not queueing/retries.

Real temporary launch/admission ledgers and owner submit entrypoint; no deployed claim.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state_runtime import get_session_admission
from tests.tools.test_thread_control_races import add_thread, admit, call

pytest_plugins = ['tests.tools.test_thread_control_races']


@pytest.fixture(autouse=True)
def create_capable_owner(owner_env, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway import run

    env = owner_env
    store = SessionStore(env.home / 'sessions', GatewayConfig())
    runner = env.authority.runner
    runner.session_store, runner._session_db, runner.adapters = store, env.db, {}
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    runner._resolve_session_agent_runtime = lambda **kwargs: ('test-model', {})
    runner._evict_cached_agent = lambda route: None
    monkeypatch.setattr(run, '_load_gateway_config', lambda *args: {
        'model': {'default': 'test-model', 'provider': 'custom'}})
    yield
    store.close_all_db_handles()


@pytest.mark.parametrize('scope', ['chat', 'box'])
@pytest.mark.parametrize('action', ['create', 'send'])
def test_old_working_launches_block_create_and_idle_send(owner_env, monkeypatch, scope, action):
    from tools import thread_tool as tool

    env = owner_env
    monkeypatch.setattr(tool, 'MAX_ACTIVE_PER_CHAT', 1 if scope == 'chat' else 10)
    monkeypatch.setattr(tool, 'MAX_ACTIVE_BOX', 1 if scope == 'box' else 10)
    env.db.create_session('other', 'tui', user_id=env.subject)
    add_thread(env, 'busy', launcher='parent' if scope == 'chat' else 'other', age_s=10 * 86400)
    admit(env, 'thread-working', sid='busy')
    add_thread(env, 'idle', age_s=10 * 86400)
    args = {'title': 'extra', 'brief': 'work'} if action == 'create' else {'thread_id': 'idle', 'message': 'resume'}
    out = call(action=action, **args)
    assert out.get('error') == 'fanout_limit', out
    assert ('of this chat' if scope == 'chat' else 'across the box') in out['message']
    assert not env.calls, 'a refused activation reached the owner'
    assert not env.db._read_all("SELECT admission_id FROM session_admissions WHERE target_session_id='idle'")


def test_idle_send_resumes_below_cap_then_refuses_another_activation(owner_env, monkeypatch):
    from tools import thread_tool as tool

    env = owner_env
    monkeypatch.setattr(tool, 'MAX_ACTIVE_PER_CHAT', 1)
    add_thread(env, 'first')
    add_thread(env, 'second')
    accepted = call(action='send', thread_id='first', message='resume', request_id='resume')
    assert accepted['success'], accepted
    row = get_session_admission(env.db, admission_id=accepted['admission_id'])
    assert row['target_session_id'] == 'first' and row['status'] == 'queued'
    refused = call(action='send', thread_id='second', message='resume')
    assert refused.get('error') == 'fanout_limit', refused
    replay = call(action='send', thread_id='first', message='resume', mode='interrupt', request_id='resume')
    assert replay['reused'] and replay['admission_id'] == accepted['admission_id']
    assert not [m for m, _ in env.calls if m == 'session.interrupt']
    # Queueing onto an already-working thread consumes no new working-thread slot.
    queued = call(action='send', thread_id='first', message='follow-up', request_id='follow-up')
    assert queued['success'], queued
    assert get_session_admission(env.db, admission_id=queued['admission_id'])['status'] == 'queued'


def test_send_is_not_charged_against_the_hourly_create_budget(owner_env, monkeypatch):
    from tools import thread_tool as tool

    monkeypatch.setattr(tool, 'MAX_CREATES_PER_HOUR', 0)
    add_thread(owner_env)
    out = call(action='send', thread_id='child', message='resume')
    assert out['success'], out
    refused = call(action='create', title='extra', brief='work')
    assert refused.get('error') == 'rate_limit'


def test_create_retry_at_cap_replays_instead_of_creating_new_work(owner_env, monkeypatch):
    from tools import thread_tool as tool

    env = owner_env
    monkeypatch.setattr(tool, 'MAX_ACTIVE_PER_CHAT', 1)
    first = call(action='create', title='work', brief='check the result', request_id='same-create')
    assert first['success'], first
    again = call(action='create', title='work', brief='check the result', request_id='same-create')
    assert again['success'] and again['reused'], again
    assert again['admission_id'] == first['admission_id'] and again['thread_id'] == first['thread_id']
    refused = call(action='create', title='other work', brief='check another result', request_id='new-create')
    assert refused.get('error') == 'fanout_limit', refused
    assert len(env.db._read_all('SELECT admission_id FROM session_admissions')) == 1


def test_concurrent_idle_sends_serialize_check_and_admission(owner_env, monkeypatch):
    from tools import thread_tool as tool
    import contextlib

    env = owner_env
    monkeypatch.setattr(tool, 'MAX_ACTIVE_PER_CHAT', 1)
    add_thread(env, 'first')
    add_thread(env, 'second')
    checked, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    real_check, real_lock = tool._check_caps, tool._create_lock
    checks = []

    def check(*args, **kwargs):
        real_check(*args, **kwargs)
        checks.append(threading.current_thread().name)
        if len(checks) == 1:
            checked.set()
            assert release.wait(10), 'first admission was not released'

    @contextlib.contextmanager
    def lock():
        if checked.is_set():
            second_entered.set()
        with real_lock():
            yield

    monkeypatch.setattr(tool, '_check_caps', check)
    monkeypatch.setattr(tool, '_create_lock', lock)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(call, action='send', thread_id='first', message='resume')
        try:
            assert checked.wait(10), 'idle send bypassed the shared cap check'
            second = pool.submit(call, action='send', thread_id='second', message='resume')
            assert second_entered.wait(10), 'second send did not enter the shared admission lock'
            assert len(checks) == 1, 'second send passed its cap check before first admission'
        finally:
            release.set()
        assert first.result(timeout=10)['success']
        assert second.result(timeout=10).get('error') == 'fanout_limit'
    assert len(env.db._read_all('SELECT admission_id FROM session_admissions')) == 1
