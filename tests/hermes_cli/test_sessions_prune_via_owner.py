"""`hermes sessions prune` while the always-on gateway owns state.db (live smoke LS-F7).

The gateway is always up, so a CLI prune that refuses whenever a process holds the store (and
deletes under it with ``--force``) is unusable or a second writer. With a ready owner the CLI only
reads its preview; the owner deletes through ``session.prune`` with the housekeeping retirement
path, which keeps a retained reset child's logical owner. A REAL foreign process holds the store,
as the gateway does; the owner end is the production ``AuthorityConnection`` dispatch.
"""
import argparse
import asyncio
import json
import threading
import subprocess
import sys
import time
from argparse import Namespace
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import pytest

import hermes_state_runtime as rt
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_cli import sessions_cmd
from hermes_state import SessionDB
from tests.hermes_state.test_target_advance_fence import _local_session, _mutate

pytestmark = pytest.mark.platforms("posix")  # the holder scan the old path refused on is POSIX-only

_HOLDER = ("import sqlite3, sys\nconn = sqlite3.connect(sys.argv[1])\n"
           "conn.execute('SELECT count(*) FROM sqlite_master')\nprint('ready', flush=True)\nsys.stdin.readline()\n")


def _aged(db, sid, days, *, ended=True):
    at = time.time() - days * 86400

    def write(conn):  # last activity = freshest of row activity and the latest message
        conn.execute('UPDATE sessions SET started_at=?, last_activity_at=?, ended_at=? WHERE id=?',
                     (at, at, at if ended else None, sid))
        conn.execute('UPDATE messages SET timestamp=? WHERE session_id=?', (at, sid))
    db._execute_write(write)


def _connection(authority, *, operator, native=True):
    identity = {'user_id': 'local-operator', 'provider': 'local', 'profile_id': authority.profile_id,
                'instance_id': authority.instance_id, 'native_bootstrap': native,
                'capabilities': ['session:create', 'session:read', 'session:submit', 'session:control']}
    return AuthorityConnection(authority, object(), identity, operator=operator)


@pytest.fixture
def owned_store(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(db_path=tmp_path / 'state.db')
    epoch = rt.begin_runtime_epoch(db, instance_id='owner')
    retired = []
    runner = SimpleNamespace(_draining=False, session_store=SimpleNamespace(retire_runtime_sessions=retired.extend))
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='owner', db=db, epoch=epoch)
    owner = _local_session(db, epoch, tmp_path)
    child = _mutate(db, epoch, owner, 'reset')['target_session_id']  # ends `owner`, keeps it as the FIFO owner
    db.append_message(child, 'user', 'kept turn')
    _aged(db, owner, 200)
    for sid, source, days in (('old-cli', 'cli', 200), ('old-cron', 'cron', 200), ('fresh-cli', 'cli', 1)):
        db.create_session(sid, source)
        db.append_message(sid, 'user', sid)
        _aged(db, sid, days)
    authority.sessions['old-cli'] = LiveSession(None, 'route-old-cli')  # resident in owner memory
    with closing(db):
        yield SimpleNamespace(db=db, authority=authority, owner=owner, child=child, retired=retired,
                              path=tmp_path / 'state.db')


def test_prune_under_the_live_owner_is_applied_by_the_owner_not_refused(owned_store, monkeypatch, capsys):
    from hermes_cli import gateway_client
    from hermes_cli import gateway_runtime
    import hermes_state
    from hermes_cli.gateway_client import rpc_error
    store = owned_store
    holder = subprocess.Popen([sys.executable, '-c', _HOLDER, str(store.path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == 'ready'
    opened = []
    real_db = hermes_state.SessionDB

    def recording_db(*args, **kwargs):
        opened.append(kwargs.get('read_only', False))
        return real_db(*args, **kwargs)

    @asynccontextmanager
    async def owner_socket(*_args, **_kwargs):
        connection = _connection(store.authority, operator=True)

        async def rpc(method, _timeout=None, **params):
            frame = await connection.dispatch({'id': 1, 'method': method, 'params': params})
            if 'error' in frame:
                raise rpc_error(frame['error'])
            return frame['result']
        try:
            yield SimpleNamespace(rpc=rpc)
        finally:
            await connection.close()

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda home, **_: gateway_runtime.GatewayDiscovery('ready', endpoint=object()))
    monkeypatch.setattr(gateway_client, 'connect_gateway', owner_socket)
    monkeypatch.setattr(hermes_state, 'SessionDB', recording_db)
    args = Namespace(sessions_action='prune', dry_run=False, yes=True, force=False, never_active=False,
                     include_archived=False, include_pinned=False,
                     **{name: None for name in sessions_cmd._FILTER_ARGS})
    args.older_than = '90'
    try:
        assert sessions_cmd.cmd_sessions(args) in (None, 0)
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    out = capsys.readouterr().out
    assert 'Refusing' not in out and 'Pruned 2 session(s).' in out, out
    db = store.db
    # --older-than honoured; the retained reset child's logical owner and its history survive.
    assert db.get_session('old-cli') is None and db.get_session('old-cron') is None
    assert db.get_session('fresh-cli') is not None and db.get_session(store.owner) is not None
    assert [m['content'] for m in db.get_messages(store.child)] == ['kept turn']
    # The CLI never opened a writer; the owner retired the deleted ids from its own memory too.
    assert opened == [True], opened
    assert 'old-cli' not in store.authority.sessions and set(store.retired) == {'old-cli', 'old-cron'}


@pytest.mark.asyncio
@pytest.mark.parametrize('grant', ['viewer', 'remote_operator'])
async def test_owner_prune_is_local_operator_only(owned_store, grant):
    """Store-wide deletion across every principal's rows: only the native local operator ticket."""
    connection = _connection(owned_store.authority, operator=grant == 'remote_operator',
                             native=grant != 'remote_operator')
    try:
        refused = await connection.dispatch({'id': 1, 'method': 'session.prune',
                                             'params': {'filters': {'older_than_days': 0}}})
    finally:
        await connection.close()
    assert refused['error']['message'] == 'permission_denied', refused
    assert owned_store.db.get_session('old-cli') is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('days', [-1, '30d'])
async def test_owner_rejects_invalid_retention_without_expanding_scope(owned_store, days):
    reply = await _prune(owned_store, filters={'older_than_days': days})
    assert reply['error']['message'] == 'invalid_params'
    assert owned_store.db.get_session('old-cli') is not None


def _args(*flags):
    from hermes_cli.subcommands.sessions import build_sessions_parser
    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(dest='command'), cmd_sessions=sessions_cmd.cmd_sessions)
    return parser.parse_args(['sessions', 'prune', *flags])


async def _prune(store, **params):
    connection = _connection(store.authority, operator=True)
    try:
        return await connection.dispatch({'id': 1, 'method': 'session.prune', 'params': params})
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_owner_batches_and_retires_each_committed_chunk(owned_store, monkeypatch):
    store = owned_store
    for i in range(5):
        sid = f'batch-{i}'
        store.db.create_session(sid, 'tool')
        store.db.append_messages_batch(sid, [{'role': 'user', 'content': 'x'} for _ in range(200)])
        _aged(store.db, sid, 100)
    calls, finishes = [], []
    real, finish = store.db.prune_sessions, store.db.finish_prune_sessions

    def batch(**kw):
        assert threading.current_thread() is not threading.main_thread()
        calls.append(kw)
        result = real(**kw)
        assert result['messages_deleted'] <= 200
        return result

    def finishing(**kw):
        assert threading.current_thread() is not threading.main_thread()
        finishes.append(True)
        return finish(**kw)

    monkeypatch.setattr(store.db, 'prune_sessions', batch)
    monkeypatch.setattr(store.db, 'finish_prune_sessions', finishing)
    reply = await _prune(store, filters={'older_than_days': 30, 'source': 'tool'})
    assert reply['result']['deleted'] == 5, reply
    assert len(calls) == 5 and finishes == [True]
    assert set(store.retired) == {f'batch-{i}' for i in range(5)}


@pytest.mark.asyncio
@pytest.mark.parametrize('drain', ['gateway', 'profile'])
async def test_drain_finishes_current_batch_and_skips_global_cleanup(owned_store, monkeypatch, drain):
    store = owned_store
    for i in range(2):
        store.db.create_session(f'drain-{i}', 'tool')
        store.db.append_messages_batch(f'drain-{i}', [{'role': 'user', 'content': 'x'} for _ in range(200)])
        _aged(store.db, f'drain-{i}', 100)
    real = store.db.prune_sessions
    calls = []

    def batch(**kw):
        result = real(**kw)
        calls.append(result)
        if drain == 'profile':
            store.authority.retiring = True
        else:
            store.authority.runner._draining = True
        return result

    monkeypatch.setattr(store.db, 'prune_sessions', batch)
    monkeypatch.setattr(store.db, 'finish_prune_sessions', lambda **_: pytest.fail('cleanup during drain'))
    reply = await _prune(store, filters={'older_than_days': 30, 'source': 'tool'})
    assert reply['result']['deleted'] == 1, reply
    assert reply['result']['remaining'] == 1 and reply['result']['interrupted'] is True
    assert len(calls) == 1 and len(store.retired) == 1


@pytest.mark.parametrize('archived', [False, True])
def test_exact_ids_preview_and_local_delete_agree(owned_store, tmp_path, capsys, archived):
    store = owned_store
    store.db._execute_write(lambda conn: conn.execute("UPDATE sessions SET archived=1 WHERE id='old-cron'"))
    ids = tmp_path / 'ids.txt'
    ids.write_text('old-cron\nold-cli\nunknown\nold-c\n', encoding='utf-8')
    flags = ['--ids-file', str(ids), '--source', 'cron', '--older-than', '30d']
    if archived:
        flags += ['--include-archived']
    args = _args(*flags, '--dry-run', '--json')
    assert sessions_cmd._cmd_prune_or_archive(store.db, args, 'prune') in (None, 0)
    preview = json.loads(capsys.readouterr().out)
    assert {row['id'] for row in preview['candidates']} == ({'old-cron'} if archived else set())
    assert set(preview['skipped_ids']) == ({'old-cli', 'unknown', 'old-c'} if archived else {'old-cron', 'old-cli', 'unknown', 'old-c'})
    assert preview['skipped'] == len(preview['skipped_ids'])
    if archived:
        assert {'id', 'source', 'ended_at', 'pinned', 'message_count'} <= preview['candidates'][0].keys()
    assert store.db.get_session('old-cron') is not None
    args = _args(*flags, '--yes', '--json')
    assert sessions_cmd._cmd_prune_or_archive(store.db, args, 'prune') in (None, 0)
    result = json.loads(capsys.readouterr().out)
    assert set(result['deleted_ids']) == {row['id'] for row in preview['candidates']}
    assert store.db.get_session('old-cli') is not None


@pytest.mark.parametrize('missing', ['source', 'idle_threshold'])
def test_sweep_requires_explicit_scope(owned_store, capsys, missing):
    flags = ['--sweep-stale-open', '--yes']
    flags += ['--source', 'tool'] if missing == 'idle_threshold' else ['--idle-threshold', '30d']
    args = _args(*flags)
    assert sessions_cmd._cmd_prune_or_archive(owned_store.db, args, 'prune') == 1
    assert '--source' in capsys.readouterr().out or missing == 'idle_threshold'


@pytest.mark.asyncio
async def test_sweep_scoped_pinned_and_live_lease_protected(owned_store):
    store = owned_store
    for sid, source in [('stale', 'tool'), ('human', 'telegram'), ('pinned', 'tool'), ('leased', 'tool')]:
        store.db.create_session(sid, source)
        store.db.append_message(sid, 'user', sid)
        _aged(store.db, sid, 45, ended=False)
    store.db.set_session_pinned('pinned', True)
    assert store.db.try_acquire_session_turn_lease('leased', 'live', ttl_seconds=300)
    reply = await _prune(store, filters={'older_than_days': 30, 'source': 'tool'},
                         sweep_idle_seconds=30 * 86400)
    assert reply['result']['closed_ids'] == ['stale'], reply
    assert reply['result']['deleted'] == 0
    assert store.db.get_session('stale')['end_reason'] == 'startup_orphan_reap'
    for sid in ['human', 'pinned', 'leased']:
        assert store.db.get_session(sid)['ended_at'] is None


def test_sweep_dry_run_is_strictly_read_only(owned_store, capsys):
    store = owned_store
    store.db.create_session('stale', 'tool')
    _aged(store.db, 'stale', 45, ended=False)
    args = _args('--sweep-stale-open', '--source', 'tool', '--idle-threshold', '30d',
                 '--older-than', '30d', '--dry-run', '--json')
    with closing(SessionDB(store.path, read_only=True)) as reader:
        assert sessions_cmd._cmd_prune_or_archive(reader, args, 'prune') in (None, 0)
    preview = json.loads(capsys.readouterr().out)
    assert preview['closed_ids'] == ['stale'] and preview['candidates'] == []
    assert store.db.get_session('stale')['ended_at'] is None


def test_owner_timeout_is_transport_budget_not_rpc_parameter(monkeypatch):
    from hermes_cli import gateway_client, sessions_owner
    budgets = []

    @asynccontextmanager
    async def socket(*_, **__):
        async def rpc(method, _timeout=None, **params):
            budgets.append((_timeout, params))
            return {'deleted': 0}
        yield SimpleNamespace(rpc=rpc)

    monkeypatch.setattr(gateway_client, 'connect_gateway', socket)
    sessions_owner.owner_prune(object(), timeout=5, filters={})
    sessions_owner.owner_prune(object(), filters={})
    assert budgets == [(5, {'filters': {}}), (600, {'filters': {}})]


@pytest.mark.asyncio
async def test_exact_owner_set_continues_zero_deleted_batch_and_retires_fenced_rows(owned_store, monkeypatch):
    store = owned_store
    store.db.create_session('oversized', 'tool')
    store.db.append_messages_batch('oversized', [{'role': 'user', 'content': 'x'} for _ in range(201)])
    _aged(store.db, 'oversized', 100)
    store.authority.sessions['oversized'] = LiveSession(None, 'route-oversized')
    original, calls = store.db.prune_sessions, []

    def batch(**kw):
        if calls:
            assert 'oversized' not in store.authority.sessions
            assert 'oversized' in store.retired
            assert store.db.get_session('oversized') is not None
        result = original(**kw)
        calls.append((kw, result))
        return result

    monkeypatch.setattr(store.db, 'prune_sessions', batch)
    reply = await _prune(store, filters={'older_than_days': 30, 'source': 'tool', 'min_messages': 200},
                         session_ids=['oversized', 'unknown', 'old-cli'])
    result = reply['result']
    assert result['deleted_ids'] == ['oversized'] and result['messages_deleted'] == 201
    assert result['skipped_ids'] == ['old-cli', 'unknown'] and result['skipped'] == 2
    assert len(calls) == 2 and calls[0][1]['deleted'] == 0
    assert calls[1][0]['session_ids'] == ['oversized']


@pytest.mark.parametrize('kind', ['local', 'compression'])
def test_preview_matches_whole_group_delete_and_partial_ids_skip(owned_store, capsys, tmp_path, kind):
    from tests.hermes_state.test_prune_bounded_batches import local_group, ended
    store = owned_store
    if kind == 'local':
        local_group(store.db, ['group-a', 'group-b'])
    else:
        ended(store.db, 'group-a')
        ended(store.db, 'group-b', parent_session_id='group-a')
        store.db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='group-a'")
    ids = tmp_path / 'ids.txt'
    ids.write_text('group-a\n', encoding='utf-8')
    flags = ['--ids-file', str(ids), '--source', 'cron', '--json']
    sessions_cmd._cmd_prune_or_archive(store.db, _args(*flags, '--dry-run'), 'prune')
    preview = json.loads(capsys.readouterr().out)
    assert preview['candidates'] == [] and preview['skipped_ids'] == ['group-a']
    ids.write_text('group-a\ngroup-b\n', encoding='utf-8')
    sessions_cmd._cmd_prune_or_archive(store.db, _args(*flags, '--dry-run'), 'prune')
    preview = json.loads(capsys.readouterr().out)
    assert {row['id'] for row in preview['candidates']} == {'group-a', 'group-b'}
    sessions_cmd._cmd_prune_or_archive(store.db, _args(*flags, '--yes'), 'prune')
    result = json.loads(capsys.readouterr().out)
    assert set(result['deleted_ids']) == {row['id'] for row in preview['candidates']}


@pytest.mark.parametrize('guard', ['lease', 'compression', 'unknown', 'queued', 'expired'])
def test_preview_busy_guards_match_delete_without_reclaiming(owned_store, tmp_path, capsys, guard):
    store = owned_store
    sid = 'busy'
    store.db.create_session(sid, 'tool')
    if guard in ('lease', 'expired'):
        assert store.db.try_acquire_session_turn_lease(sid, 'holder', ttl_seconds=300)
        if guard == 'expired':
            store.db._write_sql('UPDATE session_turn_leases SET expires_at=1 WHERE conversation_id=?', (sid,))
    elif guard == 'compression':
        assert store.db.try_acquire_compression_lock(sid, 'holder', ttl_seconds=300)
    else:
        rt.admit_session_input(store.db, epoch=store.authority.epoch, principal_id='p', session_id=sid,
                               request_id='r', payload={'text': 'x'})
        if guard == 'unknown':
            rt.claim_session_input(store.db, epoch=store.authority.epoch, session_id=sid)
            rt.recover_session_inputs(store.db, epoch=rt.begin_runtime_epoch(store.db, instance_id='new'))
    _aged(store.db, sid, 100)
    ids = tmp_path / 'ids.txt'
    ids.write_text(sid, encoding='utf-8')
    flags = ['--ids-file', str(ids), '--json']
    with closing(SessionDB(store.path, read_only=True)) as reader:
        sessions_cmd._cmd_prune_or_archive(reader, _args(*flags, '--dry-run'), 'prune')
    preview = json.loads(capsys.readouterr().out)
    expected = {sid} if guard == 'expired' else set()
    assert {row['id'] for row in preview['candidates']} == expected
    if guard == 'expired':
        assert store.db._read_one('SELECT 1 FROM session_turn_leases WHERE conversation_id=?', (sid,))
    sessions_cmd._cmd_prune_or_archive(store.db, _args(*flags, '--yes'), 'prune')
    result = json.loads(capsys.readouterr().out)
    assert set(result['deleted_ids']) == expected


@pytest.mark.asyncio
async def test_disconnected_prune_is_tracked_through_drain(owned_store, monkeypatch):
    from gateway.run_runtime import _authority_tasks
    from gateway.session_runtime_workers import mutation_tasks
    from tests.waiting import async_wait_until
    store = owned_store
    entered, release = threading.Event(), threading.Event()
    original = store.db.prune_sessions

    def batch(**kw):
        entered.set()
        assert release.wait(5)
        return original(**kw)

    monkeypatch.setattr(store.db, 'prune_sessions', batch)
    caller = asyncio.create_task(_prune(store, filters={'older_than_days': 30, 'source': 'cron'}))
    try:
        assert await async_wait_until(entered.is_set, timeout=5)
        work = mutation_tasks(store.authority)
        assert len(work) == 1 and work[0] in _authority_tasks(store.authority)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert mutation_tasks(store.authority) == work
        store.authority.runner._draining = True
        release.set()
        result = await asyncio.wait_for(asyncio.shield(work[0]), 5)
        assert result['deleted_ids'] == ['old-cron'] and result['interrupted']
        assert store.db.get_session('old-cron') is None
    finally:
        release.set()
        await asyncio.gather(caller, *mutation_tasks(store.authority), return_exceptions=True)


def test_slow_owner_timeout_at_five_seconds(monkeypatch):
    from hermes_cli import gateway_client, sessions_owner

    @asynccontextmanager
    async def socket(*_, **__):
        async def rpc(method, _timeout=None, **params):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=_timeout)
        yield SimpleNamespace(rpc=rpc)

    monkeypatch.setattr(gateway_client, 'connect_gateway', socket)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        sessions_owner.owner_prune(object(), timeout=_args('--timeout', '5').timeout, filters={})
    assert 5 <= time.monotonic() - started < 12


@pytest.mark.asyncio
@pytest.mark.parametrize('include_archived', [False, True])
async def test_owner_exact_ids_include_archived_and_source_intersect(owned_store, include_archived):
    store = owned_store
    store.db.set_session_archived('old-cron', True)
    reply = await _prune(store, filters={'older_than_days': 30, 'source': 'cron',
                                        'archived': None if include_archived else False},
                         session_ids=['old-cli', 'old-cron'])
    result = reply['result']
    assert result['deleted_ids'] == (['old-cron'] if include_archived else [])
    assert result['skipped_ids'] == (['old-cli'] if include_archived else ['old-cli', 'old-cron'])
    assert store.db.get_session('old-cli') is not None


@pytest.mark.parametrize('selection', ['sweep', 'sweep_empty_ids', 'source_only', 'sweep_age', 'sweep_ids'])
def test_sweep_cli_requires_explicit_delete_selector(owned_store, capsys, tmp_path, selection):
    store = owned_store
    for sid, days, ended in [('stale', 45, False), ('fresh-ended', 1, True), ('old-ended', 100, True)]:
        store.db.create_session(sid, 'tool')
        _aged(store.db, sid, days, ended=ended)
    flags = ['--source', 'tool', '--json']
    if selection != 'source_only':
        flags += ['--sweep-stale-open', '--idle-threshold', '30d']
    if selection in {'sweep_empty_ids', 'sweep_ids'}:
        ids = tmp_path / 'delete-ids.txt'
        ids.write_text('old-ended\n' if selection == 'sweep_ids' else '', encoding='utf-8')
        flags += ['--ids-file', str(ids)]
    if selection == 'sweep_age':
        flags += ['--older-than', '30d']
    expected = {'fresh-ended', 'old-ended'} if selection == 'source_only' else (
        {'old-ended'} if selection in {'sweep_age', 'sweep_ids'} else set())
    assert sessions_cmd._cmd_prune_or_archive(store.db, _args(*flags, '--dry-run'), 'prune') in (None, 0)
    preview = json.loads(capsys.readouterr().out)
    assert {row['id'] for row in preview['candidates']} == expected
    assert sessions_cmd._cmd_prune_or_archive(store.db, _args(*flags, '--yes'), 'prune') in (None, 0)
    result = json.loads(capsys.readouterr().out)
    assert set(result['deleted_ids']) == expected
    assert result['closed_ids'] == ([] if selection == 'source_only' else ['stale'])
    assert store.db.get_session('stale') is not None


@pytest.mark.asyncio
async def test_owner_source_only_sweep_keeps_ended_sessions(owned_store):
    store = owned_store
    store.db.create_session('stale', 'cron')
    _aged(store.db, 'stale', 45, ended=False)
    reply = await _prune(store, filters={'source': 'cron'}, sweep_idle_seconds=30 * 86400)
    assert reply['result']['closed_ids'] == ['stale']
    assert reply['result']['deleted_ids'] == []
    assert store.db.get_session('old-cron') is not None


def test_sweep_cli_never_deletes_just_closed_row_even_without_age_filter(owned_store, capsys):
    store = owned_store
    store.db.create_session('stale', 'tool')
    _aged(store.db, 'stale', 45, ended=False)
    args = _args('--sweep-stale-open', '--idle-threshold', '30d', '--source', 'tool', '--yes', '--json')
    assert sessions_cmd._cmd_prune_or_archive(store.db, args, 'prune') in (None, 0)
    result = json.loads(capsys.readouterr().out)
    assert result['closed_ids'] == ['stale'] and result['deleted_ids'] == []
    assert store.db.get_session('stale')['end_reason'] == 'startup_orphan_reap'


def test_empty_home_dry_json_and_ids_never_create_store(tmp_path, monkeypatch, capsys):
    from hermes_cli import sessions_owner
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(sessions_owner, 'live_owner', lambda: None)
    ids = tmp_path / 'ids.txt'
    ids.write_text('unknown\n', encoding='utf-8')
    assert sessions_cmd.cmd_sessions(_args('--dry-run', '--json', '--ids-file', str(ids))) in (None, 0)
    result = json.loads(capsys.readouterr().out)
    assert result['candidates'] == [] and result['skipped_ids'] == ['unknown']
    assert not (tmp_path / 'state.db').exists()


@pytest.mark.parametrize('flags', [('--timeout', '0'), ('--timeout', 'nan'),
                                   ('--idle-threshold', '30d'),
                                   ('--never-active', '--ids-file', 'missing')])
def test_invalid_prune_options_refuse_without_deleting(owned_store, capsys, flags):
    assert sessions_cmd._cmd_prune_or_archive(owned_store.db, _args(*flags, '--yes'), 'prune') == 1
    assert 'Error:' in capsys.readouterr().out
    assert owned_store.db.get_session('old-cli') is not None
