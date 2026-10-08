"""Artifact versions read model and route over synthetic read-only stores (U25)."""
import sqlite3

import pytest

from plugins.cloudseed_mobile import deliverables_index as ix
from plugins.cloudseed_mobile.dashboard import plugin_api as api
from tests.plugins.test_cloudseed_mobile import client, P  # noqa: F401  (fixture)


def page(title, body='<p>row</p>'):
    return '<!doctype html><html><head><title>' + title + '</title></head><body>' + body * 20 + '</body></html>'


def fence(html):
    return 'Here it is:\n```html\n' + html + '\n```'


def store(home, sessions, messages):
    with sqlite3.connect(home/'state.db') as db:
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,parent_session_id TEXT,end_reason TEXT,archived INTEGER,source TEXT,model_config TEXT)')
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL,tool_calls TEXT,tool_call_id TEXT,tool_name TEXT,active INTEGER,message_uid TEXT)')
        for sid, parent, end_reason, source, model_config in sessions:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,0,?,?)', (sid, 'Chat '+sid, '/nowhere', parent, end_reason, source, model_config))
    add(home, messages)


def add(home, messages):
    with sqlite3.connect(home/'state.db') as db:
        for mid, sid, content, timestamp in messages:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,NULL,NULL,NULL,1,NULL)', (mid, sid, 'assistant', content, timestamp))


def index(home, max_sessions=50):
    # The same store the routes open, so route reads see this index's generation.
    built = api.deliverable_store('default')
    assert built.home == home
    built.refresh(max_sessions=max_sessions)
    return built


def inline(result):
    return [r for r in result['items'] if r['kind'] == 'inline_content']


V1, V2, V3 = page('Pricing page', '<p>one</p>'), page('Pricing page', '<p>two</p>'), page('Pricing page', '<p>three</p>')


def test_versions_group_in_message_order_and_recent_collapses(client):
    c, home = client
    store(home, [('s', None, None, None, None)],
          [(1, 's', fence(V1), 100.0), (2, 's', fence(V2), 200.0), (3, 's', fence(V3), 300.0),
           (4, 's', fence(page('Other page')), 150.0)])
    built = index(home)
    assert built.snapshot()['scan_version'] == 6
    recent = inline(built.query())
    pricing = [r for r in recent if r['artifact_title'] == 'Pricing page']
    assert len(pricing) == 1 and len(recent) == 2
    newest = pricing[0]
    assert (newest['version_index'], newest['version_count']) == (3, 3)
    assert newest['display_name'] == 'Pricing page.html' and newest['artifact_kind'] == 'html'
    assert newest['artifact_slug'] == 'html:html:pricing-page' and 'artifact_first_seen' not in newest
    # The chat's own list keeps every version, each marked with its place.
    chat = inline(built.query(session_id='s'))
    assert sorted((r['version_index'], r['version_count']) for r in chat if r['artifact_title'] == 'Pricing page') == [(1, 3), (2, 3), (3, 3)]
    versions = c.get(P+'/deliverables/versions', params={'profile': 'default', 'artifact_key': newest['artifact_key']}).json()
    assert [v['version_index'] for v in versions['items']] == [1, 2, 3]
    assert [v['observed_at'] for v in versions['items']] == [100.0, 200.0, 300.0]
    assert [v['message_id'] for v in versions['items']] == ['1', '2', '3']
    assert versions['items'][-1]['id'] == newest['id'] and versions['version_count'] == 3
    assert {v['stored_session_id'] for v in versions['items']} == {'s'}
    assert versions['items'][0]['byte_size'] == len(V1.encode())
    assert set(versions['items'][0]) == {'id', 'version_index', 'version_hash', 'observed_at', 'stored_session_id', 'message_id', 'byte_size'}
    for version, body in zip(versions['items'], (V1, V2, V3)):
        assert c.get(P+'/deliverables/content', params={'profile': 'default', 'id': version['id']}).json()['content'] == body


def test_compressed_continuation_keeps_one_artifact_across_scan_order(client):
    c, home = client
    # The newest session is scanned first, before its compressed parent is known.
    store(home, [('a', None, 'compression', None, None), ('b', 'a', 'compression', None, None), ('c', 'b', None, None, None)],
          [(1, 'a', fence(V1), 100.0), (2, 'b', fence(V2), 200.0), (3, 'c', fence(V3), 300.0)])
    built = index(home, max_sessions=1)
    for _ in range(2): built.refresh(max_sessions=1)
    rows = inline(built.query(session_id='c'))
    assert len({r['artifact_key'] for r in rows}) == 1 and len(rows) == 3
    recent = inline(built.query())
    assert len(recent) == 1 and recent[0]['stored_session_id'] == 'c' and recent[0]['version_count'] == 3
    versions = built.versions(recent[0]['artifact_key'])
    assert [v['stored_session_id'] for v in versions['items']] == ['a', 'b', 'c']


def test_delegate_child_and_unrelated_chats_are_separate_artifacts(client):
    c, home = client
    store(home, [('p', None, 'compression', None, None), ('d', 'p', None, 'desktop', '{"_delegate_from":"p"}'),
                 ('x', None, None, None, None)],
          [(1, 'p', fence(V1), 100.0), (2, 'd', fence(V2), 200.0), (3, 'x', fence(V3), 300.0)])
    built = index(home)
    keys = {r['stored_session_id']: r['artifact_key'] for r in inline(built.query(session_id='p')) + inline(built.query(session_id='d')) + inline(built.query(session_id='x'))}
    assert len(set(keys.values())) == 3
    # The delegate's version is not Recent (subagent), so the parent's stays listed.
    assert {r['stored_session_id'] for r in inline(built.query())} == {'p', 'x'}


def test_backfilled_older_message_sorts_by_message_order(client):
    c, home = client
    store(home, [('s', None, None, None, None)], [(1, 's', fence(V2), 200.0), (2, 's', fence(V3), 300.0)])
    built = index(home)
    assert inline(built.query())[0]['version_count'] == 2
    # Registered last, but its message is the oldest: it becomes v1, never "Latest".
    add(home, [(9, 's', fence(V1), 100.0)])
    built.refresh(max_sessions=50)
    key = inline(built.query())[0]['artifact_key']
    items = built.versions(key)['items']
    assert [v['message_id'] for v in items] == ['9', '1', '2'] and [v['version_index'] for v in items] == [1, 2, 3]
    newest = inline(built.query())
    assert len(newest) == 1 and newest[0]['version_index'] == 3 and newest[0]['message_id'] == '2'


def test_repeated_body_keeps_its_first_place(client):
    c, home = client
    store(home, [('a', None, 'compression', None, None), ('b', 'a', None, None, None)],
          [(1, 'a', fence(V1), 100.0), (2, 'a', fence(V2), 200.0), (3, 'b', fence(V1), 300.0)])
    built = index(home)
    recent = inline(built.query())
    # Desktop's registry: a known body is a no-op, so v2 stays the latest version.
    assert len(recent) == 1 and recent[0]['version_index'] == 2 and recent[0]['version_count'] == 2
    items = built.versions(recent[0]['artifact_key'])['items']
    assert [(v['version_index'], v['stored_session_id'], v['message_id']) for v in items] == [(1, 'a', '1'), (2, 'a', '2')]
    assert sorted(r['version_index'] for r in inline(built.query(session_id='b'))) == [1, 1, 2]


def test_recent_search_lists_newest_matching_version(client):
    c, home = client
    store(home, [('a', None, 'compression', None, None), ('b', 'a', None, None, None)],
          [(1, 'a', fence(V1), 100.0), (2, 'b', fence(V2), 200.0)])
    with sqlite3.connect(home/'state.db') as db:
        db.execute("UPDATE sessions SET title='Launch plan' WHERE id='a'")
    built = index(home)
    # Only the older chat's title matches, so its version is the one listed.
    found = inline(built.query(q='launch'))
    assert len(found) == 1 and found[0]['stored_session_id'] == 'a' and found[0]['version_index'] == 1
    assert len(inline(built.query(q='pricing'))) == 1


def test_versions_route_caps_at_twenty_and_guards_owner_and_input(client, monkeypatch):
    c, home = client
    store(home, [('s', None, None, None, None)],
          [(i, 's', fence(page('Pricing page', f'<p>{i}</p>')), float(i)) for i in range(1, 26)])
    built = index(home)
    key = inline(built.query())[0]['artifact_key']
    params = {'profile': 'default', 'artifact_key': key}
    result = c.get(P+'/deliverables/versions', params=params).json()
    assert len(result['items']) == 20 and result['partial'] is True and result['version_count'] == 25
    assert [v['version_index'] for v in result['items']] == list(range(6, 26))
    unknown = c.get(P+'/deliverables/versions', params={**params, 'artifact_key': 'f'*64})
    assert unknown.status_code == 200 and unknown.json()['items'] == []
    assert c.get(P+'/deliverables/versions', params={**params, 'artifact_key': 'f'*129}).status_code == 400
    assert c.get(P+'/deliverables/versions', params={'profile': 'default'}).status_code == 422
    assert c.get(P+'/deliverables/versions', params={**params, 'profile': 'missing'}).status_code == 404
    monkeypatch.setattr(api, 'owner_config', lambda: {'owner_user_id': 'another', 'owner_provider': 'basic'})
    assert c.get(P+'/deliverables/versions', params=params).status_code == 403
    c.headers.pop('Authorization')
    assert c.get(P+'/deliverables/versions', params=params).status_code == 401


def test_scan_version_bump_rebuilds_a_v5_cache(client):
    c, home = client
    store(home, [('s', None, None, None, None)], [(1, 's', fence(V1), 100.0)])
    built = index(home)
    with built.connection() as db:
        db.execute("UPDATE meta SET value='5' WHERE key='scan_version'")
        # A v5 projection: inline rows without artifact fields, published under v5's fingerprint.
        db.execute('UPDATE entries SET data=json_remove(data,\'$.artifact_key\')')
        db.execute("UPDATE segments SET fingerprint='v5'")
    assert built.snapshot()['refresh_pending'] is True
    built.refresh(max_sessions=50)
    assert built.snapshot()['scan_version'] == 6
    assert inline(built.query())[0]['version_count'] == 1


def test_capability_advertises_artifact_versions():
    from tui_gateway import server
    result = server._methods['mobile.capabilities'](1, {})['result']
    assert result['protocol_version'] == 1 and result['feature_versions']['artifact_versions'] == 1


@pytest.mark.parametrize('segment', [None, 1], ids=['one-segment', 'message-per-segment'])
def test_body_repeated_in_one_chat_keeps_its_first_place(client, monkeypatch, segment):
    c, home = client
    if segment: monkeypatch.setattr(ix, 'MAX_MESSAGES', segment)
    store(home, [('s', None, None, None, None)], [(1, 's', fence(V1), 100.0), (2, 's', fence(V2), 200.0), (3, 's', fence(V1), 300.0)])
    built = index(home)
    for _ in range(3): built.refresh(max_sessions=50)
    recent = inline(built.query())
    assert len(recent) == 1 and (recent[0]['version_index'], recent[0]['version_count']) == (2, 2)
    assert [v['message_id'] for v in built.versions(recent[0]['artifact_key'])['items']] == ['1', '2']


def test_artifact_lookup_uses_its_partial_index_not_a_generation_scan(client):
    c, home = client
    store(home, [('s', None, None, None, None)], [(1, 's', fence(V1), 100.0)])
    built = index(home)
    with built.connection() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM entries WHERE generation=? AND kind='inline_content' AND json_extract(data,'$.artifact_key') IN (?)", ('g', 'k')).fetchall()
    assert any('entries_artifact' in str(step) for step in plan)
