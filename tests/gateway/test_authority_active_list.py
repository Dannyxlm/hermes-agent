"""``session.active_list`` is answered by the session authority from its own live turns.

Desktop's silent-turn check (``session-states.ts`` ``onEventSilence``) asks this verb whether a
turn that went 45 s without an event is still running. The authority never registered it, so the
WS fell back to the legacy sidecar's handler, whose ``_sessions`` never holds an authority-owned
session: a foreground tool quiet for 45 s read as a reaped turn, and Desktop painted "Hermes ended
this turn without a reply" over a turn that was still running.
"""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_authority import LiveSession, initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    started, release = asyncio.Event(), asyncio.Event()

    async def answer(event):
        # A long foreground tool: the turn emits nothing until it returns.
        started.set()
        await release.wait()
        return event.text

    runner = SimpleNamespace(_session_db=store._db, session_store=store, _draining=False,
                             _handle_message=answer, _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='owner')
    store._db.create_session('chat', source='telegram')
    authority.sessions['chat'] = LiveSession(SessionSource(platform=Platform.TELEGRAM, chat_id='chat'), 'chat')
    connection = AuthorityConnection(authority, object(), {'user_id': 'viewer'})

    async def active(**params):
        reply = await connection.dispatch({'id': 1, 'method': 'session.active_list', 'params': params})
        assert 'result' in reply, reply
        return {row['id']: row for row in reply['result']['sessions']}

    async def submit(text):
        from gateway.session_contract import SessionRef, Submission
        await authority.submit(connection.actor, Submission(text, SessionRef(authority.profile_id, 'chat'), {'text': text}, 'queue'))
        await asyncio.wait_for(started.wait(), 5)

    yield SimpleNamespace(authority=authority, active=active, submit=submit, release=release)
    release.set()


@pytest.mark.asyncio
async def test_a_running_turn_reads_working_until_it_settles(owner):
    assert (await owner.active())['chat']['status'] == 'idle'
    await owner.submit('hi')
    row = (await owner.active(current_session_id='chat'))['chat']
    assert (row['status'], row['session_key'], row['current']) == ('working', 'chat', True), row
    owner.release.set()
    await owner.authority.sessions['chat'].task
    assert (await owner.active())['chat']['status'] == 'idle'


@pytest.mark.asyncio
async def test_thread_provenance_comes_from_the_authority_store(owner):
    db = owner.authority.db
    db.record_launch(launch_id='thread:authority', kind='lane', launcher_profile='other',
                     launcher_session_id='opening-chat', child_profile='default')
    db.bind_launch('thread:authority', child_session_id='chat')
    assert (await owner.active())['chat'].get('thread_parent_id') == 'opening-chat'


@pytest.mark.asyncio
async def test_thread_provenance_survives_multiple_compressions_but_not_forks(owner):
    from gateway.session_authority import LiveSession
    db = owner.authority.db
    db.create_session('thread-root', source='desktop')
    db.record_launch(launch_id='thread:compressed', kind='lane', launcher_profile='default',
                     launcher_session_id='opening-chat', child_profile='default')
    db.bind_launch('thread:compressed', child_session_id='thread-root')
    for parent, child in [('thread-root', 'middle'), ('middle', 'tip')]:
        db._write_sql('UPDATE sessions SET end_reason=? WHERE id=?', ('compression', parent))
        db.create_session(child, source='desktop', parent_session_id=parent)
    db.create_session('fork', source='desktop', parent_session_id='thread-root',
                      model_config={'_branched_from': 'thread-root'})
    source = owner.authority.sessions['chat'].source
    for sid in ['tip', 'fork']:
        owner.authority.sessions[sid] = LiveSession(source, sid)
    rows = await owner.active()
    assert rows['tip'].get('thread_parent_id') == 'opening-chat'
    assert rows['fork'].get('thread_parent_id') is None
    assert rows['chat'].get('thread_parent_id') is None


@pytest.mark.asyncio
async def test_a_session_replaced_during_provenance_read_does_not_publish_the_old_snapshot(owner, monkeypatch):
    from copy import copy
    import hermes_state_launches

    def replaced_during_read(*args):
        owner.authority.sessions['chat'] = copy(owner.authority.sessions['chat'])
        return {}

    monkeypatch.setattr(hermes_state_launches, 'thread_parents', replaced_during_read, raising=False)
    assert await owner.active() == {}


@pytest.mark.asyncio
async def test_a_row_names_its_source(owner):
    # Desktop's poll can see a messaging turn settle before its messaging row loads; the source
    # on the live row is what keeps that settle from reading as a Desktop completion.
    assert (await owner.active())['chat']['source'] == 'telegram'


@pytest.mark.asyncio
async def test_a_turn_blocked_on_a_prompt_reads_waiting(owner, monkeypatch):
    await owner.submit('hi')
    controls = owner.authority.sessions['chat'].controls
    monkeypatch.setattr(controls, 'snapshot', lambda session_id, generation: ({'kind': 'approval'},))
    assert (await owner.active())['chat']['status'] == 'waiting'
