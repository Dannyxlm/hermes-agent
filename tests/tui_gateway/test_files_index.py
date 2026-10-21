"""Files 3.3 contracts through public RPCs and real isolated SQLite projections."""
import json
import sqlite3

import pytest

from hermes_state import SessionDB
from plugins.cloudseed_mobile.dashboard.plugin_api import deliverable_store
from tests.tui_gateway import test_methods_mobile as mobile

mobile_home = mobile.mobile_home
peer = mobile.peer
rpc = mobile.rpc


@pytest.fixture
def indexed(mobile_home, peer):
    home = mobile_home
    root = home.parent / 'workspaces' / 'Fixture'
    (root / 'outputs').mkdir(parents=True)
    (root / 'outputs' / 'report.md').write_text('report')
    (root / 'outputs' / 'hero.png').write_bytes(b'image')
    (root / 'outputs' / 'private').mkdir()
    (root / 'outputs' / 'private' / 'secret.md').write_text('private')
    (root / 'escape').symlink_to(home, target_is_directory=True)
    cfg = ('cloudseed_mobile:\n  owner_provider: fixture-auth\n  owner_user_id: fixture-owner\n'
           '  workspace_root: ' + str(root.parent) + '\n')
    (home / 'config.yaml').write_text(cfg)
    (home / 'profiles' / 'ops' / 'config.yaml').write_text(
        'cloudseed_mobile:\n  owner_provider: foreign\n  owner_user_id: intruder\n')
    peer.auth_identity = {'provider': 'fixture-auth', 'user_id': 'fixture-owner'}
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session('tip', 'desktop', parent_session_id='root')
        db.create_session('delegate', 'subagent', parent_session_id='root')
        db.create_session('other', 'desktop')
        db._conn.execute("UPDATE sessions SET cwd=?,title='Fixture chat ' || id", (str(root),))
        db._conn.execute("UPDATE sessions SET end_reason='compression' WHERE id='root'")
        db.append_message('root', 'assistant', 'MEDIA: ' + str(root / 'outputs/report.md'))
        db.append_message('tip', 'user', json.dumps([
            {'type': 'file', 'file': {'path': str(root / 'outputs/hero.png')}},
            {'type': 'text', 'text': '[User site](https://example.com/user)'}]))
        db.append_message('tip', 'assistant', '[Agent site](https://example.com/agent)')
        db.append_message('delegate', 'assistant', '[Delegate](https://example.com/delegate)')
        db.append_message('other', 'assistant', '[Other](https://example.com/other)')
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("UPDATE messages SET timestamp='unknown' WHERE role='user'")
    index = deliverable_store('default')
    for _ in range(10):
        index.refresh(max_sessions=100)
        if not index.snapshot()['refresh_pending']:
            break
    return home, root, index, index.workspaces[0]['id']


def result(method='files.index', **params):
    response = rpc(method, profile='default', **params)
    assert 'result' in response, response
    return response['result']


def test_user_attachment_attribution_and_complete_full_query_counts(indexed):
    home, root, _index, _wid = indexed
    original = (home / 'state.db').read_bytes()
    page = result(session_id='root', limit=1)
    assert len(page['items']) == 1 and page['next_cursor']
    assert page['totals'] == {'media': 1, 'files': 1, 'links': 2, 'messages': 3}
    following = result(session_id='root', limit=1, cursor=page['next_cursor'], snapshot_id=page['snapshot_id'])
    rows = page['items'] + following['items']
    attachment = next(r for r in rows if r['display_name'] == 'hero.png')
    assert attachment['author_kind'] == 'me' and attachment['author_profile'] is None
    assert attachment['observed_at'] is None and attachment['message_id']
    assert attachment['mention_text'] == '@file:' + str(root / 'outputs/hero.png')
    assert attachment['collection'] is None and attachment['is_directory'] is False
    assert (home / 'state.db').read_bytes() == original
    assert result(session_id='root', category='media')['totals']['media'] == 1
    assert result(session_id='root', author='me')['totals']['files'] == 0


@pytest.mark.parametrize('session_id', ['missing', 'ops-root', ''])
def test_unknown_explicit_session_never_broadens(indexed, session_id):
    assert rpc('files.index', profile='default', session_id=session_id)['error']['code'] == 4400


@pytest.mark.parametrize('identity', [None, {'provider': 'foreign', 'user_id': 'intruder'},
                                       {'provider': 'internal', 'user_id': 'service'}])
def test_owner_admission(indexed, peer, identity):
    peer.auth_identity = identity
    assert rpc('files.index', profile='default')['error']['code'] == 4403


def test_installation_owner_not_profile_override_and_concrete_profiles(indexed, peer):
    peer.auth_identity = {'provider': 'foreign', 'user_id': 'intruder'}
    assert rpc('files.index', profile='ops')['error']['code'] == 4403
    peer.auth_identity = {'provider': 'fixture-auth', 'user_id': 'fixture-owner'}
    assert 'result' in rpc('files.index', profile='ops')
    for profile in ['all', 'missing', '../ops', '']:
        assert 'error' in rpc('files.index', profile=profile)


def test_cursor_scope_and_common_snapshot(indexed):
    home, _root, index, wid = indexed
    page = result(session_id='root', limit=1)
    common = result('links.index', session_id='root', snapshot_id=page['snapshot_id'])
    assert common['snapshot_id'] == page['snapshot_id']
    assert common['totals'] == page['totals']
    for changed in [{'q': 'x'}, {'author': 'me'}, {'session_id': 'other'}, {'category': 'media'},
                    {'workspace_id': wid}, {'include_workspace': True, 'workspace_id': wid}]:
        params = {'session_id': 'root', 'cursor': page['next_cursor'], **changed}
        assert rpc('files.index', profile='default', **params)['error']['code'] == 4091
    assert rpc('links.index', profile='default', session_id='root', cursor=page['next_cursor'])['error']['code'] == 4091
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', '[New](https://example.com/new)')
    index.refresh(max_sessions=100)
    assert rpc('files.index', profile='default', snapshot_id=page['snapshot_id'])['error']['code'] == 4091


def test_workspace_suggestions_safe_children_not_delivery_totals(indexed):
    _home, root, _index, wid = indexed
    page = result(workspace_id=wid, include_workspace=True)
    folder = next(r for r in page['items'] if r['kind'] == 'workspace_directory')
    assert folder['mention_text'] == '@folder:' + str(root / 'outputs')
    assert folder['message_id'] is None and folder['observed_at'] is None
    assert all(r['display_name'] != 'escape' for r in page['items'])
    assert page['totals']['files'] == 1 and page['totals']['media'] == 1
    for path in ['../', 'escape', 'outputs/private']:
        assert 'error' in rpc('files.index', profile='default', workspace_id=wid,
                              include_workspace=True, directory_path=path)
    assert rpc('files.index', profile='default', workspace_id='foreign')['error']['code'] == 4403


def test_empty_and_partial_counts(indexed):
    home, _root, _index, _wid = indexed
    page = result(q='no such synthetic item')
    assert page['items'] == [] and page['next_cursor'] is None
    assert page['totals'] == dict(media=0, files=0, links=0, messages=0)
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', 'not indexed yet')
    page = result(q='no such synthetic item')
    assert not page['coverage']['complete'] and page['coverage']['refresh_pending']
    assert all(v is None for v in page['totals'].values())


def test_display_metadata_attachments_and_canonical_quoted_mentions(indexed):
    home, root, index, _wid = indexed
    target = root / 'outputs' / 'a report.md'
    target.write_text('report')
    with SessionDB(db_path=home / 'state.db') as db:
        mid = db.append_message('root', 'user', 'See attachment')
        db._conn.execute('UPDATE messages SET display_metadata=? WHERE id=?',
                         (json.dumps({'attachments': [{'type': 'file', 'path': str(target)}]}), mid))
        db._conn.commit()
    index.refresh(max_sessions=100)
    page = result(session_id='root', q='a report.md')
    assert len(page['items']) == 1
    row = page['items'][0]
    assert row['author_kind'] == 'me'
    from agent.context_references import parse_context_references
    refs = parse_context_references(row['mention_text'])
    assert refs[0].target == str(target)


def test_bounded_attachment_evidence_reports_unknown_totals(indexed):
    home, root, index, _wid = indexed
    with SessionDB(db_path=home / 'state.db') as db:
        mid = db.append_message('root', 'user', 'Attached')
        metadata = {'attachments': [{'path': str(root / 'outputs/report.md')}] * 513}
        db._conn.execute('UPDATE messages SET display_metadata=? WHERE id=?', (json.dumps(metadata), mid))
        db._conn.commit()
    index.refresh(max_sessions=100)
    page = result(session_id='root')
    assert not page['coverage']['complete']
    assert all(value is None for value in page['totals'].values())


def test_live_session_revocation_and_read_does_not_need_state_write_lock(indexed):
    home, _root, _index, _wid = indexed
    with sqlite3.connect(home / 'state.db') as writer:
        writer.execute('BEGIN IMMEDIATE')
        assert result(session_id='root')['items']
        writer.rollback()
        writer.execute("UPDATE sessions SET archived=1 WHERE id IN ('root','tip')")
    assert rpc('files.index', profile='default', session_id='root')['error']['code'] == 4400
    stale = result()
    assert stale['items'] == [] and stale['totals']['files'] is None
    assert result()['totals']['files'] == 0


@pytest.mark.parametrize('params', [{'limit': True}, {'limit': 201}, {'q': 'x' * 257},
                                  {'author': 'someone'}, {'include_workspace': 'true'}])
def test_invalid_queries_are_errors_not_empty_pages(indexed, params):
    assert rpc('files.index', profile='default', **params)['error']['code'] == 4400


def test_cursor_invalidation_on_source_change_before_refresh(indexed):
    home, _root, _index, _wid = indexed
    page = result(limit=1)
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', 'Source changed')
    assert rpc('links.index', profile='default', snapshot_id=page['snapshot_id'])['error']['code'] == 4091
