"""The restricted widget reader uses real profile storage, RPC and HTTP authority."""
import uuid
import base64
import json

import pytest
from fastapi.testclient import TestClient

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tests.tui_gateway.test_methods_mobile_push import push_service
from tui_gateway.mobile_widget_inbox import inbox_snapshot, WidgetInboxChanged


def register(peer, profile="ops", **overrides):
    peer.auth_identity = {"user_id": "fixture-owner", "provider": "fixture-auth"}
    params = dict(profile=profile, installation_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()), read_token="12" * 32)
    params.update(overrides)
    result = rpc("mobile.widget.inbox.register", **params)
    assert "result" in result, result
    return params, result["result"]


def test_full_inbox_pages_read_markers_and_scoped_rotation(push_service, mobile_home, peer, monkeypatch):
    import os
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home
    from tui_gateway import mobile_widget_inbox
    monkeypatch.setattr("pathlib.Path.home", lambda: mobile_home.parent)
    (mobile_home / 'profiles' / 'ops' / '.env').write_text('WIDGET_INBOX_SCOPE_FIXTURE=secondary-only\n')
    before = dict(os.environ)
    observed = []
    original = mobile_widget_inbox.page_replies
    def scoped_replies(db, rows):
        observed.append((Path(get_hermes_home()).resolve(), get_secret('WIDGET_INBOX_SCOPE_FIXTURE')))
        return original(db, rows)
    from pathlib import Path
    monkeypatch.setattr(mobile_widget_inbox, 'page_replies', scoped_replies)
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        for n in range(125):
            sid = f"chat-{n}"
            db.create_session(sid, "desktop")
            db.append_message(sid, "assistant", f"fixture {n}")
        db._conn.execute("UPDATE sessions SET last_read_at=0 WHERE id LIKE 'chat-%'")
        db._conn.execute("UPDATE sessions SET hidden=1 WHERE id='chat-0'")
        db._conn.execute("UPDATE sessions SET archived=1 WHERE id='chat-1'")
        db._conn.commit()
    params, receipt = register(peer)
    assert receipt['profile'] == 'ops'
    first = inbox_snapshot(push_service.store, params['read_token'])
    assert len(first['items']) == 100 and first['next_cursor']
    second = inbox_snapshot(push_service.store, params['read_token'], first['next_cursor'])
    rows = first['items'] + second['items']
    assert len({r['session_id'] for r in rows}) == 124
    assert second['next_cursor'] is None
    assert all(r['reply']['unread'] for r in rows if r['session_id'].startswith('chat-'))
    assert not any(r['session_id'] in {'chat-0', 'chat-1', 'root'} for r in rows)
    with SessionDB(db_path=path) as db:
        db._conn.execute("UPDATE sessions SET last_read_at=9999999999 WHERE id='chat-124'")
        db._conn.commit()
    with pytest.raises(WidgetInboxChanged):
        inbox_snapshot(push_service.store, params['read_token'], first['next_cursor'])
    changed = inbox_snapshot(push_service.store, params['read_token'])
    assert next(r for r in changed['items'] if r['session_id'] == 'chat-124')['reply']['unread'] is False
    # A -> B -> A changes grant identity; no old cursor can cross a profile switch.
    register(peer, **{**params, 'profile': 'default'})
    default = inbox_snapshot(push_service.store, params['read_token'])
    assert [r['session_id'] for r in default['items']] == ['root']
    register(peer, **params)
    with pytest.raises(WidgetInboxChanged):
        inbox_snapshot(push_service.store, params['read_token'], changed['next_cursor'])
    assert inbox_snapshot(push_service.store, params['read_token'])['profile'] == 'ops'
    assert observed[0] == (path.parent.resolve(), 'secondary-only')
    assert (mobile_home.resolve(), None) in observed
    assert observed[-1] == observed[0]
    assert dict(os.environ) == before


def test_inbox_capability_cannot_expand_authority_and_logout_wins(push_service, mobile_home, peer, monkeypatch):
    from hermes_cli import web_server
    from tui_gateway import mobile_widget_inbox
    monkeypatch.setattr("pathlib.Path.home", lambda: mobile_home.parent)
    params, _ = register(peer)
    monkeypatch.setattr(web_server.app.state, 'auth_required', False, raising=False)
    client = TestClient(web_server.app)
    headers = {'Authorization': 'Bearer ' + params['read_token']}
    route = '/api/mobile/widgets/snapshot'
    response = client.get(route + '?inbox=1', headers=headers)
    assert response.status_code == 200
    assert response.json()['profile'] == 'ops'
    assert response.headers['cache-control'] == 'no-store'
    assert client.get(route + '?inbox=1&cursor=invalid', headers=headers).status_code == 400
    page = response.json()
    stale_cursor = base64.urlsafe_b64encode(json.dumps({
        'offset': 100, 'grant': page['grant_id'], 'revision': 'older-revision',
        'epoch': page['inbox_summary_scope'].get('pending_epoch'),
    }).encode()).decode()
    assert client.get(route, params={'inbox': '1', 'cursor': stale_cursor}, headers=headers).status_code == 409
    for query in ('?inbox=1&profile=default', '?inbox=1&path=/etc/passwd', '?inbox=1&inbox=1', '?cursor=invalid'):
        assert client.get(route + query, headers=headers).status_code == 400
    for path in ('/api/sessions', '/api/config', route + '/extra'):
        assert client.get(path, headers=headers).status_code == 401
    assert client.post(route + '?inbox=1', headers=headers).status_code == 401
    peer.auth_identity = None
    assert rpc('mobile.widget.inbox.register', **params)['error']['code'] == 4403
    peer.auth_identity = {"user_id": "fixture-owner", "provider": "fixture-auth"}
    assert 'error' in rpc('mobile.widget.inbox.register', **{**params, 'profile': '../ops'})
    identity = {k: params[k] for k in ('installation_id', 'connection_id')}
    original = mobile_widget_inbox.page_replies
    def revoke(db, rows):
        result = original(db, rows)
        rpc('mobile.widget.unregister', **identity)
        return result
    monkeypatch.setattr(mobile_widget_inbox, 'page_replies', revoke)
    assert client.get(route + '?inbox=1', headers=headers).status_code == 401
    assert push_service.store.widget_reader(params['read_token']) is None
