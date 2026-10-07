"""Files › Recent lists only files that exist in their workspace now (Iris Gate B #2).

Path text cut out of tool output or code (`+str(generation`, `example.pdf'`) used to
reach Recent as "Delivered" files. Chat-scoped history still shows every reference.
"""
import sqlite3
from plugins.cloudseed_mobile import deliverables_index as ix
from tests.plugins.test_cloudseed_mobile import client, P
from tests.plugins.cloudseed_mobile.test_workspace_files import setup_root


def seed(home, root, contents):
    with sqlite3.connect(home/'state.db') as db:
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,parent_session_id TEXT,end_reason TEXT,archived INTEGER)')
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL,tool_calls TEXT,tool_call_id TEXT,tool_name TEXT,active INTEGER,message_uid TEXT)')
        db.execute('INSERT INTO sessions VALUES(?,?,?,NULL,NULL,0)', ('s', 'Synthetic', str(root)))
        for i, (role, content, tool) in enumerate(contents):
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,NULL,NULL,?,1,?)', (i, 's', role, content, 100.0 + i, tool, f'm{i}'))


def recent(index, **kwargs):
    return {r['relative_path'] for r in index.query(**kwargs)['items']}


def test_recent_drops_fragments_and_missing_files_but_chat_history_keeps_them(client):
    c, home = client
    root, wid = setup_root(c, home)
    (root/'outputs/real.pdf').write_bytes(b'%PDF synthetic')
    grep_output = "test.py:12:    assert x == 'MEDIA:+str(generation'\nMEDIA:outputs/example.pdf'\nMEDIA:" + str(root/'outputs/real.pdf')
    seed(home, root, [('tool', grep_output, 'terminal'), ('assistant', 'MEDIA: ' + str(root/'outputs/report.md'), None)])
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    index.refresh()
    assert recent(index) == {'outputs/real.pdf'}
    history = recent(index, session_id='s')
    assert 'outputs/real.pdf' in history and 'outputs/report.md' in history and len(history) >= 3
    (root/'outputs/report.md').write_text('# now saved')
    assert recent(index) == {'outputs/real.pdf', 'outputs/report.md'}
    (root/'outputs/real.pdf').unlink()
    assert recent(index) == {'outputs/report.md'}


def test_recent_never_follows_a_symlink_out_of_the_workspace(client):
    c, home = client
    root, wid = setup_root(c, home)
    outside = home/'outside'; outside.mkdir(); (outside/'secret.md').write_text('not granted')
    (root/'outputs/link.md').symlink_to(outside/'secret.md')
    (root/'outputs/dir.md').mkdir()
    seed(home, root, [('assistant', 'MEDIA: ' + str(root/'outputs/link.md'), None),
                      ('assistant', 'MEDIA: ' + str(root/'outputs/dir.md'), None)])
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    index.refresh()
    assert recent(index) == set()


def test_recent_pages_past_missing_rows_without_gaps_or_repeats(client):
    c, home = client
    root, wid = setup_root(c, home)
    contents = []
    for i in range(12):
        if i % 3 == 0: (root/f'outputs/{i}.md').write_text('saved')
        contents.append(('assistant', 'MEDIA: ' + str(root/f'outputs/{i}.md'), None))
    seed(home, root, contents)
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    index.refresh()
    seen = []; cursor = None
    for _ in range(10):
        page = index.query(limit=1, cursor=cursor)
        seen += [r['relative_path'] for r in page['items']]
        cursor = page['next_cursor']
        if not cursor: break
    assert sorted(seen) == sorted(f'outputs/{i}.md' for i in range(0, 12, 3))
    assert len(seen) == len(set(seen))


def test_recent_route_applies_the_same_check(client):
    c, home = client
    root, wid = setup_root(c, home)
    (root/'outputs/real.md').write_text('saved')
    seed(home, root, [('assistant', 'MEDIA: ' + str(root/'outputs/real.md') + '\nMEDIA: ' + str(root/'outputs/ghost.md'), None)])
    c.get(P + '/deliverables?profile=default')
    body = c.get(P + '/deliverables?profile=default').json()
    assert {r['relative_path'] for r in body['items']} == {'outputs/real.md'}


def test_recent_drops_remote_media_cut_out_of_code(client):
    c, home = client
    root, wid = setup_root(c, home)
    test_source = "assert d.project(...)[0]['kind']=='remote_media'  # MEDIA:https://cdn.example.com/x.png'}])[0]['kind']=='remote_media'"
    seed(home, root, [('tool', test_source, 'terminal'), ('tool', 'MEDIA:https://cdn.example.com/real.png', 'image_generate')])
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    index.refresh()
    urls = {r.get('url') for r in index.query()['items']}
    assert urls == {'https://cdn.example.com/real.png'}
