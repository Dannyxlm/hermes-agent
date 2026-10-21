"""Mixed link evidence is admitted from saved user/assistant messages, not tools."""
import json
import sqlite3

from hermes_state import SessionDB
from tests.tui_gateway import test_files_index as file_tests

indexed = file_tests.indexed
mobile_home = file_tests.mobile_home
peer = file_tests.peer
result = file_tests.result
from tests.tui_gateway.test_methods_mobile import rpc


def test_user_agent_links_lineage_and_no_delegate_leak(indexed):
    page = result('links.index', session_id='root')
    assert {r['url'] for r in page['items']} == {'https://example.com/user', 'https://example.com/agent'}
    user = next(r for r in page['items'] if r['author_kind'] == 'me')
    assert user['title'] == 'User site' and user['tag'] is None
    assert user['mention_text'] == '@url:https://example.com/user'
    assert user['stored_session_id'] == 'tip' and user['observed_at'] is None
    assert len(result('links.index', session_id='root', author='agents')['items']) == 1
    assert len(result('links.index', session_id='root', author='me')['items']) == 1


def test_links_deduplicate_urls_and_count_distinct_messages(indexed):
    home, _root, index, _wid = indexed
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', '[Again](https://example.com/agent) https://example.com/agent')
    index.refresh(max_sessions=100)
    page = result('links.index', session_id='root', q='example.com', limit=1)
    assert len(page['items']) == 1 and page['next_cursor']
    assert page['totals'] == dict(media=0, files=0, links=2, messages=3)
    other = result('links.index', session_id='root', q='example.com', limit=1,
                   cursor=page['next_cursor'], snapshot_id=page['snapshot_id'])
    assert page['items'][0]['id'] != other['items'][0]['id']


def test_no_reasoning_tool_output_malicious_urls_or_private_file_paths(indexed):
    home, root, index, _wid = indexed
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'tool', 'https://example.com/tool-page', tool_name='web_extract')
        db.append_message('root', 'assistant', '<think>https://example.com/reasoning</think> ' +
                          '[bad](javascript:alert(1)) https://user:pass@example.com/credential ' +
                          'http://127.0.0.1/private https://localhost/private')
        db.append_message('root', 'user', json.dumps([
            {'type': 'reasoning', 'text': 'https://example.com/hidden'},
            {'type': 'file', 'file': {'path': str(root / 'outputs/private/secret.md')}},
            {'type': 'file', 'file': {'path': str(root / '.env')}}]))
    index.refresh(max_sessions=100)
    page = result('links.index', session_id='root')
    assert {r['url'] for r in page['items']} == {'https://example.com/user', 'https://example.com/agent'}
    files = result(session_id='root')
    assert {r['display_name'] for r in files['items']} == {'hero.png', 'report.md'}
    assert all('secret' not in json.dumps(r) for r in files['items'])


def test_link_ids_are_not_byte_grants_and_old_rest_query_is_file_only(indexed):
    _home, _root, index, _wid = indexed
    page = result('links.index')
    assert page['items']
    assert all(r['kind'] != 'link' for r in index.query()['items'])
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException):
        index.file_target(page['items'][0]['id'])
    assert rpc('links.index', profile='default', include_workspace=True)['error']['code'] == 4000


def test_workspaces_counts_from_same_projection(indexed):
    from plugins.cloudseed_mobile.dashboard.plugin_api import file_workspaces
    rows = file_workspaces('default')['items']
    assert rows[0]['indexed_item_count'] == 5


def test_symlink_reference_cannot_grant_a_composer_expansion(indexed):
    home, _root, index, _wid = indexed
    (home / 'outside.md').write_text('outside the workspace')
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', '[Escape](escape/outside.md)')
    index.refresh(max_sessions=100)
    page = result(session_id='root', q='outside.md')
    assert all(row['mention_text'] is None for row in page['items'])


def test_explicit_link_preserves_stored_query_and_fragment_text(indexed):
    home, _root, index, _wid = indexed
    url = 'https://example.com/resource?next=%2Ffolder%2F&bang=!#part?'
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'user', '[Exact URL](' + url + ')')
    index.refresh(max_sessions=100)
    page = result('links.index', session_id='root', q='resource')
    assert [row['url'] for row in page['items']] == [url]
    assert page['items'][0]['title'] == 'Exact URL'


def test_same_url_author_filters_and_snapshot_principal_binding(indexed, peer):
    home, _root, index, _wid = indexed
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'user', 'https://example.com/agent')
    index.refresh(max_sessions=100)
    mine = result('links.index', session_id='root', author='me')
    assert {r['url'] for r in mine['items']} == {'https://example.com/user', 'https://example.com/agent'}
    page = result(limit=1)
    path = home / 'config.yaml'
    path.write_text(path.read_text().replace('fixture-owner', 'new-owner'))
    peer.auth_identity = {'provider': 'fixture-auth', 'user_id': 'new-owner'}
    assert rpc('files.index', profile='default', cursor=page['next_cursor'])['error']['code'] == 4091
