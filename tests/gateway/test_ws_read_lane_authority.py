"""The WebSocket read lane on a session-authority socket (Desktop's shared socket, native tickets).

Every handler on the authority was awaited on the socket's serial lane, so ``ping`` and every other
cheap request queued behind a slow projection such as ``profiles.list``. Read-only projections now run
beside the lane, and still see what the frames before them wrote, still wait in a starting profile's
hold lane, and resolve a sibling profile's routed connection once even from concurrent workers.

The real ``handle_ws`` lanes and a real multiplexing runtime (default and ``alpha`` served, ``beta``
held mid-start by the shared rig); only a projection's body is held on a finite gate.
"""
import asyncio
import threading

import pytest

from tests.gateway.test_sibling_attach_while_profile_starting import rig  # the profile-start rig fixture
from tests.waiting import async_wait_until

GATE_S = 10.0


def _rpc(sock, request_id, method, **params):
    sock.inbound.put_nowait({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})


@pytest.mark.asyncio
async def test_ping_and_a_fast_read_are_answered_while_profiles_list_runs(rig, monkeypatch):  # health: allow F811 -- `rig` is the profile-start rig imported above
    from gateway import session_group_controls
    entered, release = threading.Event(), threading.Event()
    real = session_group_controls._profiles

    def held_profiles(*args, **kwargs):
        entered.set()
        assert release.wait(GATE_S), 'profiles.list gate never released'
        return real(*args, **kwargs)

    monkeypatch.setattr(session_group_controls, '_profiles', held_profiles)
    sock = await rig.connect()
    _rpc(sock, 'roster', 'profiles.list')
    try:
        await async_wait_until(entered.is_set, timeout=10)
        _rpc(sock, 'ping', 'ping')
        _rpc(sock, 'live', 'session.active_list')
        ping, live = await sock.reply('ping', timeout=10), await sock.reply('live', timeout=10)
        assert ping['result'] == {'pong': True}
        assert 'sessions' in live['result'], live
        assert 'roster' not in sock.replies
    finally:
        release.set()
    roster = await sock.reply('roster')
    assert 'profiles' in roster['result'], roster


@pytest.mark.asyncio
async def test_a_read_sent_after_a_create_lists_the_new_session(rig):  # health: allow F811 -- `rig` is the profile-start rig imported above
    sock = await rig.connect()
    rig.create(sock, 'alpha', 'made')
    _rpc(sock, 'listed', 'session.active_list', profile='alpha')
    made, listed = await sock.reply('made', timeout=30), await sock.reply('listed', timeout=30)
    assert 'result' in made, made
    assert made['result']['session_id'] in {row['id'] for row in listed['result']['sessions']}, listed


@pytest.mark.asyncio
async def test_a_read_naming_a_starting_profile_waits_for_the_start(rig):  # health: allow F811 -- `rig` is the profile-start rig imported above
    """The hold lane applies to reads too: refused ``runtime_draining`` at once on the read lane
    before, now answered by the started profile, while a frame for a running profile goes first."""
    sock = await rig.connect()
    _rpc(sock, 'beta-read', 'session.context_breakdown', profile='beta', session_id='nope')
    _rpc(sock, 'alpha-list', 'session.list', profile='alpha')
    alpha = await sock.reply('alpha-list', timeout=10)
    assert 'sessions' in alpha['result'], alpha
    assert 'beta-read' not in sock.replies  # waiting for beta, not refused
    rig.release.set()
    await asyncio.wait_for(rig.serving, 30)
    beta = await sock.reply('beta-read')
    assert beta.get('error', {}).get('data', {}).get('reason') != 'runtime_draining', beta


@pytest.mark.asyncio
async def test_concurrent_reads_resolve_one_routed_connection_per_sibling(rig, monkeypatch):  # health: allow F811 -- `rig` is the profile-start rig imported above
    """A family read resolves sibling profiles in a worker thread while other reads route on the loop:
    two resolutions at once must not build two connections (the second would stay registered for the
    sibling's events after this socket closed)."""
    from types import SimpleNamespace
    from gateway import session_local_migration
    from gateway.session_authorities import authority_for_profile_id
    from tui_gateway.ws import _authority_connection
    launch = authority_for_profile_id(rig.runner, str(rig.alpha.parent.parent.resolve()))
    ws = SimpleNamespace(scope={'hermes.session_authority': launch})
    transport = SimpleNamespace(desktop_local=False)
    identity = {'user_id': 'uid:desk', 'profile_scope': 'host', 'profile_id': launch.profile_id,
                'instance_id': launch.instance_id}
    conn = _authority_connection(ws, transport, identity, True)
    sibling = rig.runner.session_authorities.for_home(rig.alpha)
    before = len(sibling.events)
    real_bind, both = session_local_migration.bind_native_transport, threading.Barrier(2)
    built = []

    def racing_bind(*args, **kwargs):
        built.append(1)
        try:
            both.wait(timeout=1.0)  # without the lock both builders meet here; with it the wait runs out
        except threading.BrokenBarrierError:
            pass
        return real_bind(*args, **kwargs)

    monkeypatch.setattr(session_local_migration, 'bind_native_transport', racing_bind)
    got = [None, None]

    def resolve(slot):
        got[slot] = conn._sibling_for('alpha')

    threads = [threading.Thread(target=resolve, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert got[0] is got[1] is not None
    assert len(built) == 1
    assert len(sibling.events) == before + 1
    await conn.close()
    assert len(sibling.events) == before
