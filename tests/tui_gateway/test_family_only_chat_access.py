"""Removing a thread from Chats cannot remove its Family/open/read path."""
from starlette.testclient import TestClient

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import rpc

pytest_plugins = ['tests.tui_gateway.test_methods_mobile']


def test_family_retains_thread_hidden_from_chats(mobile_home, peer, monkeypatch):
    import hermes_state
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    from tui_gateway import server

    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', mobile_home / 'state.db')
    monkeypatch.setattr('hermes_cli.profiles._get_default_hermes_home', lambda: mobile_home)
    with SessionDB(db_path=mobile_home / 'state.db') as db:
        for sid in ['parent', 'child']:
            db.create_session(sid, 'desktop')
            db.set_session_title(sid, sid)
            db.append_message(sid, 'user', 'fixture message')
        db.record_launch(launch_id='thread:access', kind='lane', launcher_profile='default',
                         launcher_session_id='parent', child_profile='default')
        db.bind_launch('thread:access', child_session_id='child')
    monkeypatch.setattr(server, '_resolve_model', lambda: 'fixture')
    server._sessions['parent-live'] = {'session_key': 'parent', 'transport': peer, 'history': []}
    server._sessions['child-live'] = {'session_key': 'child', 'transport': peer, 'history': []}
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    listed = client.get('/api/sessions?profile=default').json()
    assert {row['id'] for row in listed['sessions']} == {'parent', 'root'}
    result = rpc('session.family', profile='default', session_id='parent', since=0)
    assert 'result' in result, result
    node = next(n for n in result['result']['nodes'] if n['session_id'] == 'child')
    assert node['profile'] == 'default'
    assert 'open' in node['actions']
    detail = client.get('/api/sessions/child?profile=default')
    assert detail.status_code == 200, detail.text
    assert detail.json()['id'] == 'child'
    messages = client.get('/api/sessions/child/messages?profile=default')
    assert messages.status_code == 200, messages.text
    assert any(row['content'] == 'fixture message' for row in messages.json()['messages'])
    roster = rpc('session.active_list', profile='default')['result']['sessions']
    assert any(row['session_key'] == 'child' for row in roster)
