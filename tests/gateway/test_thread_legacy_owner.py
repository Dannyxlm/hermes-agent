"""Pre-update login-stamped leads can follow up on real gateway-created threads."""
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon


@pytest.mark.parametrize('source', ['tui', 'gui'])
def test_old_login_lead_can_read_its_gateway_created_thread(tmp_path, monkeypatch, source):
    from gateway.owner_identity import host_owner_subject
    from gateway.session_identity import authenticated_subject
    from hermes_state import SessionDB
    from tools import thread_tool as tool

    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HOME', str(user))
    monkeypatch.setenv('USERPROFILE', str(user))
    monkeypatch.setattr(Path, 'home', lambda: user)
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.requests = []
    model.blocked, model.release = threading.Event(), threading.Event()
    worker = threading.Thread(target=model.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{model.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'local-wire-stub', 'base_url': base},
        'auxiliary': {'title_generation': {'enabled': False}},
        'platform_toolsets': {'gui': [], 'tui': []}, 'terminal': {'cwd': str(home)},
    }))
    env = child_env() | dict(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
        PYTHONPATH=str(root), OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=base, PYTHONUNBUFFERED='1')
    login = authenticated_subject({'provider': 'basic', 'user_id': 'fixture'})
    with SessionDB(home / 'state.db') as db:
        db.create_session('old-lead', source, user_id=login)

    try:
        with daemon(root, home, env, barrier=False):
            caller = tool._caller(SimpleNamespace(session_id='old-lead'))

            async def create(client):
                snap = await client.rpc('session.create', request_id='thread-create-legacy', source='tui',
                    started_by_session=caller.session_id, started_by_profile=caller.profile,
                    launch_kind='lane', launch_title='legacy follow-up', launch_id='thread:legacy',
                    cwd=str(home), toolsets=[])
                return snap['session_id']

            sid = tool._run(tool._with_owner(home, caller.subject, create))
            with SessionDB(home / 'state.db') as db:
                assert db.get_session(sid)['user_id'] == host_owner_subject()
                db.append_message(sid, 'assistant', 'THREAD_RESULT')
            resolved = tool._resolve(caller, sid)
            assert resolved.session_id == sid
            tool._require_own(caller, resolved, 'send')
            result = json.loads(tool.thread_tool({'action': 'read', 'thread_id': sid},
                                                parent_agent=SimpleNamespace(session_id='old-lead')))
            assert result['success'] is True, result
            assert [row['text'] for row in result['messages']] == ['THREAD_RESULT']
            with SessionDB(home / 'state.db') as db:
                assert db.get_session('old-lead')['user_id'] == login, 'reading must not rewrite old ownership'
    finally:
        model.release.set()
        model.shutdown()
        model.server_close()
        worker.join(timeout=5)
