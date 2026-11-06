"""Thread controls against the real owner/ledger, with deterministic turn transitions.

Only the socket hop and model execution are replaced: RPC dispatch, admissions, claims,
settlement, generation fencing and the thread tool are production code. Not a live canary.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.owner_identity import host_owner_subject
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_contract import SessionRef
from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, begin_runtime_epoch, claim_session_input, get_session_admission,
    settle_session_input,
)


class TurnAgent:
    def __init__(self):
        self.interrupted = False

    def interrupt(self, *args, **kwargs):
        self.interrupted = True


@pytest.fixture
def owner_env(tmp_path, monkeypatch):
    from tools import thread_tool as tool

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(tool.ts, 'homes', lambda: {'default': home})
    monkeypatch.setattr(tool.ts, 'profile_of', lambda path: 'default')
    monkeypatch.setattr(tool, '_config', dict)
    db = SessionDB(home / 'state.db')
    subject = host_owner_subject()
    db.create_session('parent', 'tui', user_id=subject)
    runner = SimpleNamespace(_draining=False, _cached_agent_for=lambda route: None)
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='test-owner', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test-owner'))
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    transport = SimpleNamespace(write=lambda frame: None)
    connection = AuthorityConnection(authority, transport, {'user_id': subject})
    env = SimpleNamespace(home=home, db=db, authority=authority, connection=connection,
                          subject=subject, transition=None, transition_at='before_resume', calls=[])

    async def rpc(method, **params):
        env.calls.append((method, params))
        if method == 'session.resume' and env.transition and env.transition_at == 'before_resume':
            env.transition()
            env.transition = None
        if method == 'session.resume':
            # Existing live routes attach directly; no legacy-history migration is under test.
            ref = SessionRef(str(home), params['session_id'])
            snapshot = await authority.attach(connection.actor, ref, history=False)
            connection.subscriptions[ref.session_id] = snapshot.subscription_id
            handle = snapshot.handle
            reply = {'result': {'session_id': ref.session_id, 'revision': handle.revision,
                                'running': handle.execution_state == 'running',
                                'execution_generation': handle.execution_generation}}
        else:
            reply = await connection.dispatch({'id': len(env.calls), 'method': method, 'params': params})
        if method == 'session.resume' and env.transition and env.transition_at == 'after_resume':
            env.transition()
            env.transition = None
        if 'error' in reply:
            from hermes_cli.gateway_client import GatewayRPCError
            error = reply['error']
            raise GatewayRPCError(error['message'], code=error['code'], data=error.get('data'))
        return reply['result']

    async def with_owner(home_, subject_, fn):
        assert home_ == home and subject_ == subject
        return await fn(SimpleNamespace(rpc=rpc))

    env.rpc = rpc
    monkeypatch.setattr(tool, '_with_owner', with_owner)
    yield env
    db.close()


def add_thread(env, sid='child', *, launcher='parent', age_s=0):
    env.db.create_session(sid, 'tui', user_id=env.subject)
    env.db.record_launch(launch_id='thread:' + sid, kind='lane', launcher_profile='default',
                         launcher_session_id=launcher, child_profile='default')
    env.db.bind_launch('thread:' + sid, child_session_id=sid)
    if age_s:
        env.db._write_sql('UPDATE work_launches SET created_at=created_at-? WHERE launch_id=?',
                          (age_s, 'thread:' + sid))
    source = SimpleNamespace(platform=None, user_id=env.subject, chat_id=sid)
    env.authority.sessions[sid] = LiveSession(source, sid + '-route')


def admit(env, request_id, *, sid='child', start=True):
    row = admit_session_input(env.db, epoch=env.authority.epoch, principal_id=env.subject,
                              session_id=sid, request_id=request_id, payload={'text': 'work'}, intent='queue')
    if start:
        row = claim_session_input(env.db, epoch=env.authority.epoch, session_id=sid)
        assert isinstance(row, dict)
        env.authority._stamp_claim(SessionRef(str(env.home), sid), env.authority.sessions[sid], row)
        agent = TurnAgent()
        env.authority.adopt_agent(sid, row['generation'], agent)
        return row, agent
    return row


def finish(env, row):
    settle_session_input(env.db, epoch=env.authority.epoch, admission_id=row['admission_id'],
                          generation=row['generation'], outcome='completed')
    env.authority.adopted.pop(row['target_session_id'], None)
    env.authority.executing.pop(row['target_session_id'], None)


def call(**args):
    from tools.registry import registry
    return json.loads(registry.dispatch('thread', args, session_id='parent'))


@pytest.mark.parametrize('action', ['stop', 'send'])
@pytest.mark.parametrize('transition_at', ['before_resume', 'after_resume'])
@pytest.mark.parametrize('force', [False, True])
def test_tool_turn_to_human_transition_never_interrupts_the_human(owner_env, action, transition_at, force):
    env = owner_env
    add_thread(env)
    first, _ = admit(env, 'thread-first')
    human = {}

    def transition():
        finish(env, first)
        human['row'], human['agent'] = admit(env, 'human-next')

    env.transition, env.transition_at = transition, transition_at
    extra = {'message': 'new direction', 'mode': 'interrupt'} if action == 'send' else {}
    out = call(action=action, thread_id='child', force=force, **extra)
    assert not human['agent'].interrupted, 'the tool stopped the successor human admission'
    assert out['success'], out
    assert get_session_admission(env.db, admission_id=human['row']['admission_id'])['status'] == 'started'
    if action == 'send':
        assert get_session_admission(env.db, admission_id=out['admission_id'])['status'] == 'queued'
        assert not out.get('note', '').startswith('Interrupted')
    else:
        assert out['status'] == 'running'


@pytest.mark.parametrize('action', ['stop', 'send'])
def test_normal_tool_turn_can_still_be_stopped(owner_env, action):
    env = owner_env
    add_thread(env)
    _, agent = admit(env, 'thread-first')
    extra = {'message': 'new direction', 'mode': 'interrupt'} if action == 'send' else {}
    out = call(action=action, thread_id='child', **extra)
    assert out['success'], out
    assert agent.interrupted
    if action == 'stop':
        assert out['status'] == 'stopping'
    else:
        assert out['note'].startswith('Interrupted')


@pytest.mark.parametrize('force', [False, True])
def test_human_turn_requires_explicit_force(owner_env, force):
    env = owner_env
    add_thread(env)
    _, agent = admit(env, 'human-first')
    out = call(action='stop', thread_id='child', force=force)
    assert out['success'], out
    assert agent.interrupted is force


@pytest.mark.parametrize('mismatch', ['admission', 'generation', 'author', 'launch', 'launcher'])
def test_owner_checks_all_thread_control_fences_before_interrupt(owner_env, mismatch):
    from hermes_cli.gateway_client import GatewayRPCError

    env = owner_env
    add_thread(env)
    row, agent = admit(env, 'human-first' if mismatch == 'author' else 'thread-first')
    control = dict(admission_id=row['admission_id'], launch_id='thread:child',
                   launcher_profile='default', launcher_session_id='parent', force=False)
    control.update({
        'admission': {'admission_id': 'another-admission'},
        'launch': {'launch_id': 'thread:another-child'},
        'launcher': {'launcher_session_id': 'another-parent'},
    }.get(mismatch, {}))
    generation = row['generation'] + int(mismatch == 'generation')

    async def request():
        await env.rpc('session.resume', session_id='child')
        await env.rpc('session.interrupt', session_id='child', execution_generation=generation,
                      thread_control=control)

    with pytest.raises(GatewayRPCError) as refused:
        asyncio.run(request())
    assert str(refused.value) == {
        'admission': 'stale_admission', 'generation': 'stale_generation',
        'author': 'permission_denied', 'launch': 'permission_denied', 'launcher': 'permission_denied',
    }[mismatch]
    assert not agent.interrupted
    assert get_session_admission(env.db, admission_id=row['admission_id'])['status'] == 'started'


@pytest.mark.parametrize('patch', [{'force': 'true'}, {'force': 1}, {'force': None}])
def test_owner_never_coerces_force_into_permission_to_stop_a_human(owner_env, patch):
    from hermes_cli.gateway_client import GatewayRPCError

    env = owner_env
    add_thread(env)
    row, agent = admit(env, 'human-first')
    control = dict(admission_id=row['admission_id'], launch_id='thread:child',
                   launcher_profile='default', launcher_session_id='parent', **patch)

    async def request():
        await env.rpc('session.resume', session_id='child')
        await env.rpc('session.interrupt', session_id='child', execution_generation=row['generation'],
                      thread_control=control)

    with pytest.raises(GatewayRPCError, match='invalid_params'):
        asyncio.run(request())
    assert not agent.interrupted
