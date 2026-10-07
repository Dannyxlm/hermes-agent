"""Recent admits real producer deliveries, not echoed source or child work."""
import sqlite3

import pytest
from fastapi import HTTPException

from plugins.cloudseed_mobile import deliverables_index as ix
from tests.plugins import test_cloudseed_mobile as mobile
from tests.plugins.cloudseed_mobile.test_workspace_files import setup_root
from tests.plugins.cloudseed_mobile.test_deliverables_index import store

client = mobile.client


def index_messages(client, messages, **session):
    c, home = client
    root, wid = setup_root(c, home)
    (root / 'outputs/real.png').write_bytes(b'fixture')
    store(home, root, 1)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('ALTER TABLE sessions ADD COLUMN source TEXT')
        db.execute('DELETE FROM messages')
        for key, value in session.items():
            db.execute(f'UPDATE sessions SET {key}=?', (value,))
        for i, message in enumerate(messages):
            content = message['content'].replace('$ROOT', str(root))
            db.execute('INSERT INTO messages(id,session_id,role,content,tool_name,active) VALUES(?,?,?,?,?,1)',
                       (i, '0', message.get('role', 'tool'), content, message.get('tool_name')))
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    index.refresh(max_sessions=10)
    return index, root, home


@pytest.mark.parametrize('tool', ['read_file', 'terminal'])
@pytest.mark.parametrize('fragment', [
    'MEDIA:+str(generation',
    "expect(parse('MEDIA:...')).toBe('MEDIA:...')",
    "expect(parse('MEDIA:download')).toEqual([])",
    "MEDIA:x.png'}])[0]['kind']=='remote_media'",
    'MEDIA: creates no entry',
    'MEDIA: directive lines outside code…',
])
def test_reader_fragments_never_enter_recent(client, tool, fragment):
    index, _, _ = index_messages(client, [{'tool_name': tool, 'content': fragment}])
    assert index.query()['items'] == []


@pytest.mark.parametrize('tool', [
    '', 'read_file', 'search_files', 'terminal', 'process', 'process_manage',
    'execute_code', 'web_extract', 'web_search', 'session_search', 'skill_view',
    'skills_list', 'read_terminal', 'read_window_below', 'browser_exec',
    'browser_cdp', 'browser_snapshot', 'browser_navigate', 'browser_console',
    'browser_click', 'browser_back', 'browser_scroll', 'browser_press',
    'browser_type', 'browser_get_images', 'mcp__reader__read_file',
    'functions.read_file', 'skill_manage', 'delegate_task',
])
def test_reader_even_standalone_existing_media_never_delivers(client, tool):
    index, _, _ = index_messages(client, [{'tool_name': tool, 'content': 'MEDIA:$ROOT/outputs/real.png'}])
    assert index.query()['items'] == []
    assert all(row['action'] == 'referenced' for row in index.query(session_id='0')['items'])


@pytest.mark.parametrize('tool,content,action', [
    ('image_generate', '{"output_path":"$ROOT/outputs/real.png","success":true}', 'created'),
    ('write_file', '{"path":"$ROOT/outputs/real.png","verified":true}', 'created'),
    ('text_to_speech', 'MEDIA:$ROOT/outputs/real.png', 'delivered'),
    ('custom_producer', 'MEDIA:$ROOT/outputs/real.png', 'delivered'),
])
def test_existing_producers_still_deliver(client, tool, content, action):
    index, _, _ = index_messages(client, [{'tool_name': tool, 'content': content}])
    assert [(row['relative_path'], row['action']) for row in index.query()['items']] == [('outputs/real.png', action)]


@pytest.mark.parametrize('content', [
    'Result MEDIA:$ROOT/outputs/real.png ready',
    '```text\nMEDIA:$ROOT/outputs/real.png\n```',
    '~~~text\nMEDIA:$ROOT/outputs/real.png\n~~~',
    '`example\nMEDIA:$ROOT/outputs/real.png\n`',
])
def test_nonreader_media_requires_standalone_line_outside_code(client, content):
    index, _, _ = index_messages(client, [{'tool_name': 'custom_producer', 'content': content}])
    assert index.query()['items'] == []


@pytest.mark.parametrize('role,tool,content', [
    ('assistant', None, 'MEDIA:$ROOT/outputs/missing.png'),
    ('tool', 'custom_producer', 'MEDIA:$ROOT/outputs/missing.png'),
    ('tool', 'image_generate', '{"success":true,"output_path":"$ROOT/outputs/missing.png"}'),
    ('tool', 'write_file', '{"verified":true,"path":"$ROOT/outputs/missing.png"}'),
])
def test_missing_local_deliveries_are_unavailable_not_recent(client, role, tool, content):
    index, _, _ = index_messages(client, [{'role': role, 'tool_name': tool, 'content': content}])
    assert index.query()['items'] == []
    rows = index.query(session_id='0')['items']
    assert len(rows) == 1 and rows[0]['availability'] == 'unavailable'
    with pytest.raises(HTTPException) as exc:
        index.file_target(rows[0]['id'])
    assert exc.value.status_code == 404


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('kind', ['file_symlink', 'directory_symlink', 'directory'])
def test_local_existence_check_never_follows_symlinks_or_accepts_directories(client, kind):
    index, root, home = index_messages(client, [])
    outside = home / 'outside'; outside.mkdir()
    (outside / 'real.png').write_bytes(b'not granted')
    path = root / 'outputs/candidate.png'
    if kind == 'file_symlink':
        path.symlink_to(outside / 'real.png')
    elif kind == 'directory_symlink':
        path.symlink_to(outside, target_is_directory=True)
        path = path / 'real.png'
    else:
        path.mkdir()
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('INSERT INTO messages(id,session_id,role,content,active) VALUES(0,?,?,?,1)',
                   ('0', 'assistant', 'MEDIA:' + str(path)))
    index.refresh()
    assert index.query()['items'] == []


@pytest.mark.parametrize('source', ['subagent', 'delegate'])
def test_subagent_source_excluded_but_parent_and_compression_reply_count(client, source):
    index, root, home = index_messages(client, [{'tool_name': 'custom_producer', 'content': 'MEDIA:$ROOT/outputs/real.png'}],
                                       parent_session_id='parent', source=source, title='Renamed worker')
    assert index.query()['items'] == []
    assert len(index.query(session_id='0')['items']) == 1
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("INSERT INTO sessions(id,title,cwd,source,end_reason) VALUES('parent','Subagent: ordinary root',?,'cli','compression')", (str(root),))
        db.execute("INSERT INTO sessions(id,title,cwd,source,parent_session_id) VALUES('continued','Continued',?,'cli','parent')", (str(root),))
        db.execute("INSERT INTO messages(id,session_id,role,content,active) VALUES(1,'continued','assistant',?,1)",
                   ('MEDIA:' + str(root / 'outputs/real.png'),))
    index.refresh()
    assert [row['stored_session_id'] for row in index.query()['items']] == ['continued']


def test_delegate_child_with_inherited_source_is_excluded(client):
    """Real delegate children often inherit the parent's surface (desktop/tui) as
    `source`; the delegation marker in model_config is the structural signal."""
    index, _, home = index_messages(client, [{'tool_name': 'custom_producer', 'content': 'MEDIA:$ROOT/outputs/real.png'}],
                                    parent_session_id='parent', source='desktop', title='Renamed worker')
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('ALTER TABLE sessions ADD COLUMN model_config TEXT')
        db.execute('UPDATE sessions SET model_config=?', ('{"_delegate_from": "parent", "max_iterations": 750}',))
    index.refresh()
    assert index.query()['items'] == []
    assert len(index.query(session_id='0')['items']) == 1


def test_scan_v4_false_tool_delivery_is_rebuilt(client, monkeypatch):
    with monkeypatch.context() as m:
        # Freeze an actual v4-style row without changing the new reader policy.
        real_project = ix.project
        def project(*args, **kwargs):
            rows = real_project(*args, **kwargs)
            for row in rows:
                row['action'] = row['outcome'] = 'delivered'
            return rows
        m.setattr(ix, 'project', project)
        index, _, _ = index_messages(client, [{'tool_name': 'read_file', 'content': 'MEDIA:$ROOT/outputs/real.png'}])
    with sqlite3.connect(index.path) as db:
        db.execute("UPDATE meta SET value='4' WHERE key='scan_version'")
    assert len(index.query()['items']) == 1
    assert index.query()['coverage']['refresh_pending']
    index.refresh()
    assert index.query()['items'] == []
    assert not index.query()['coverage']['refresh_pending']
