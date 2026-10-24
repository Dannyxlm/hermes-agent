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


def test_standalone_intake_clears_tools_environment_and_binds_child(tmp_path, monkeypatch, provenance):
    from hermes_cli import oneshot, runtime_provider, mcp_startup
    import run_agent
    db = SessionDB(tmp_path / 'state.db')
    observed = []

    class Agent:
        def __init__(self, **kwargs):
            assert not set(provenance) & os.environ.keys()
            self.session_id = kwargs.get('session_id') or 'child'
            self._session_db = kwargs['session_db']
        def _ensure_db_session(self):
            self._session_db.create_session(self.session_id, 'oneshot')
        def run_conversation(self, *a, **k):
            self._ensure_db_session()
            with db._read_ctx() as conn:
                observed.extend(dict(r) for r in conn.execute('SELECT * FROM work_launches'))
            return {'completed': True, 'final_response': 'fixture'}
        def shutdown_memory_provider(self, *a):
            pass
        def close(self):
            pass

    monkeypatch.setattr(run_agent, 'AIAgent', Agent)
    monkeypatch.setattr(oneshot, '_create_session_db_for_oneshot', lambda: db)
    monkeypatch.setattr(oneshot, '_close_agent', lambda *a: None)
    monkeypatch.setattr(mcp_startup, 'ensure_mcp_discovery_before_agent_build', lambda **k: None)
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_with_fallback', lambda *a, **k: ({'provider': 'fixture'}, None))
    try:
        answer, _ = oneshot._run_agent('fixture', model='fixture', use_config_toolsets=False)
        assert answer == 'fixture'
        assert len(observed) == 1
        row = observed[0]
        assert row['launcher_profile'] == 'launcher'
        assert row['launcher_session_id'] == 'parent'
        assert row['child_session_id'] == 'child'
        assert row['title'] == 'short task'
        assert row['kind'] == 'teammate'
        assert db.get_session('child')['parent_session_id'] is None
    finally:
        db.close()


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
