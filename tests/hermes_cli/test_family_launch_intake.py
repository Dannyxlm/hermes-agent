"""Family launch intake on both CLI transports and the canonical create transaction."""
import os
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


@pytest.fixture
def provenance(monkeypatch):
    values = {'HERMES_STARTED_BY_SESSION_ID': 'parent', 'HERMES_STARTED_BY_PROFILE': 'launcher',
              'HERMES_LAUNCH_TITLE': 'short task', 'HERMES_LAUNCH_ID': 'stable-launch'}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.mark.asyncio
async def test_gateway_launch_intake_clears_tools_environment_and_binds_child(tmp_path, monkeypatch, provenance):
    from contextlib import asynccontextmanager

    from gateway.session_controls import AuthorityConnection
    from hermes_cli import gateway_chat, launch_provenance
    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.gateway_chat_view import GatewayChatView
    from tests.gateway.test_local_source_labels import _authority

    monkeypatch.setattr(launch_provenance, '_pending', {})
    authority, _ = await _authority(tmp_path, monkeypatch)
    conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    calls, observed, children = [], [], []

    class Peer:
        async def rpc(self, method, **params):
            assert not set(provenance) & os.environ.keys()
            calls.append((method, params))
            reply = await conn.dispatch({'id': len(calls), 'method': method, 'params': params})
            assert 'result' in reply, reply
            return reply['result']

    @asynccontextmanager
    async def connected():
        assert not set(provenance) & os.environ.keys()
        yield Peer()

    async def rendered(self, query, *, oneshot):
        assert query == 'fixture' and oneshot
        assert self.unattended and self.close_on_finite
        assert not set(provenance) & os.environ.keys()
        children.append(self.session_id)
        # The real create transaction must persist the launch before any execution.
        with authority.db._read_ctx() as db:
            observed.extend(dict(row) for row in db.execute('SELECT * FROM work_launches'))
        return 0

    monkeypatch.setattr(gateway_chat, 'connect_gateway', connected)
    monkeypatch.setattr(GatewayChatView, 'run', rendered)
    args = build_top_level_parser()[0].parse_args(['-z', 'fixture', '--in', str(tmp_path)])
    try:
        assert await gateway_chat.run_gateway_chat(args) == 0
        create = next(params for method, params in calls if method == 'session.create')
        assert create['source'] == 'oneshot'
        assert {key: create[key] for key in launch_provenance.FIELDS if key in create} == dict(
            started_by_session='parent', started_by_profile='launcher',
            launch_title='short task', launch_id='stable-launch')
        assert len(observed) == 1
        row = observed[0]
        assert row['launch_id'] == 'stable-launch'
        assert row['launcher_profile'] == 'launcher'
        assert row['launcher_session_id'] == 'parent'
        assert row['child_session_id'] == children[0]
        assert row['title'] == 'short task'
        assert row['kind'] == 'teammate'
        child = authority.db.get_session(children[0])
        assert child['source'] == 'oneshot'
        assert child['parent_session_id'] is None
    finally:
        await conn.close()
        authority.db.close()


def test_cli_flags_are_accepted_and_override_environment(provenance):
    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.launch_provenance import take_launch
    parser, _, _ = build_top_level_parser()
    args = parser.parse_args(['chat', '-Q', '--started-by-session', 'flag-parent',
                              '--started-by-profile', 'flag-profile', '--launch-title', 'flag title',
                              '--launch-kind', 'lane'])
    fields = take_launch(args)
    assert fields == dict(started_by_session='flag-parent', started_by_profile='flag-profile',
                          launch_title='flag title', launch_kind='lane', launch_id='stable-launch')
    assert not set(provenance) & os.environ.keys()


@pytest.mark.asyncio
async def test_gateway_create_receipt_records_child_profile_only_and_is_idempotent(tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from tests.gateway.test_local_source_labels import _authority
    authority, _ = await _authority(tmp_path, monkeypatch)
    conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    params = dict(request_id='create', source='oneshot', cwd=str(tmp_path), started_by_session='foreign-parent',
                  started_by_profile='foreign-profile', launch_title='L' * 60, launch_id='create-launch')
    try:
        result = await conn.dispatch({'id': 1, 'method': 'session.create', 'params': params})
        assert 'result' in result, result
        sid = result['result']['stored_session_id']
        retry = await conn.dispatch({'id': 2, 'method': 'session.create', 'params': params})
        assert retry['result']['stored_session_id'] == sid
        with authority.db._read_ctx() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM work_launches')]
        assert len(rows) == 1
        assert rows[0]['child_session_id'] == sid
        assert rows[0]['launcher_profile'] == 'foreign-profile'
        assert rows[0]['title'] == 'L' * 39 + '…'
        assert authority.db.get_session(sid)['parent_session_id'] is None
        changed = await conn.dispatch({'id': 3, 'method': 'session.create',
                                       'params': dict(params, started_by_session='changed')})
        assert 'error' in changed
    finally:
        await conn.close()
        authority.db.close()


def test_resume_binding_preserves_original_anchor_across_compression(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('before-compression', 'oneshot')
        db.record_launch('restart-stable', kind='lane', launcher_profile='default',
                         launcher_session_id='launcher', child_profile='default')
        db.bind_launch('restart-stable', child_session_id='before-compression')
        db.end_session('before-compression', 'compression')
        db.create_session('after-compression', 'oneshot', parent_session_id='before-compression')
        db.bind_launch('restart-stable', child_session_id='after-compression')
        assert db._read_one('SELECT child_session_id FROM work_launches')[0] == 'before-compression'
        db.create_session('unrelated', 'oneshot')
        with pytest.raises(ValueError):
            db.bind_launch('restart-stable', child_session_id='unrelated')


@pytest.mark.asyncio
async def test_gateway_resume_binds_launch_before_the_next_admitted_turn(tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from tests.gateway.test_local_source_labels import _authority
    authority, _ = await _authority(tmp_path, monkeypatch)
    conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    try:
        created = await conn.dispatch({'id': 1, 'method': 'session.create',
                                      'params': dict(request_id='resumed-target', source='oneshot', cwd=str(tmp_path))})
        sid = created['result']['stored_session_id']
        fields = dict(session_id=sid, started_by_session='launcher', started_by_profile='foreign-profile',
                      launch_id='resumed-launch', launch_title='Resume child')
        for request_id in (2, 3):
            result = await conn.dispatch({'id': request_id, 'method': 'session.resume', 'params': fields})
            assert 'result' in result, result
        with authority.db._read_ctx() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM work_launches')]
        assert len(rows) == 1
        assert rows[0]['launch_id'] == 'resumed-launch'
        assert rows[0]['child_session_id'] == sid
        assert rows[0]['launcher_session_id'] == 'launcher'
        assert rows[0]['kind'] == 'teammate'
        assert authority.db.get_session(sid)['parent_session_id'] is None
    finally:
        await conn.close()
        authority.db.close()
