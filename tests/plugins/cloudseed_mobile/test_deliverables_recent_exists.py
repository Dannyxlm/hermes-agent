"""Files › Recent lists only files that exist in their workspace now (Iris Gate B #2).

Path text cut out of tool output or code (`+str(generation`, `example.pdf'`) used to
reach Recent as "Delivered" files. Chat-scoped history still shows every reference.
"""
import json
import sqlite3

import pytest
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
    # Reader-tool output (terminal/grep) is reference-only even when a line names a
    # real file; the assistant's own standalone directive is what delivers it.
    seed(home, root, [('tool', grep_output, 'terminal'),
                      ('assistant', 'MEDIA: ' + str(root/'outputs/real.pdf'), None),
                      ('assistant', 'MEDIA: ' + str(root/'outputs/report.md'), None)])
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
    root, _wid = setup_root(c, home)
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


def image_page(c, **params):
    response = c.get(P + '/deliverables', params={'profile': 'default', 'media_type': 'image', **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_generated_producer_receipt_reaches_route_and_original_transport(client):
    import base64
    from agent.image_gen_provider import save_b64_image, success_response
    c, home = client
    root, _ = setup_root(c, home)
    data = b'\x89PNG\r\n\x1a\nsynthetic-original'
    image = save_b64_image(base64.b64encode(data).decode(), prefix='fixture')
    receipt = success_response(image=str(image), model='fixture', prompt='synthetic',
                               aspect_ratio='square', provider='fixture')
    seed(home, root, [('tool', json.dumps(receipt), 'image_generate')])
    image_page(c)  # First read schedules the real index refresh.
    rows = image_page(c)['items']
    assert len(rows) == 1
    row = rows[0]
    assert row['relative_path'] == image.name and row['action'] == 'created'
    assert row['availability'] == 'available'
    params = {'profile': 'default', 'id': row['id']}
    url = P + '/workspace-files/download'
    assert c.get(url, params=params).content == data
    response = c.get(url, params=params, headers={'Range': 'bytes=2-5'})
    assert response.status_code == 206 and response.content == data[2:6]
    assert response.headers['content-range'] == f'bytes 2-5/{len(data)}'
    assert c.head(url, params=params).headers['content-length'] == str(len(data))
    assert c.head(url, params=params).content == b''
    assert c.get(P + '/workspace-files/read', params=params).json()['byteSize'] == len(data)
    image.unlink()
    assert image_page(c)['items'] == []
    assert c.get(url, params=params).status_code == 404


def test_image_predicate_precedes_page_window_and_binds_cursor(client):
    c, home = client
    root, wid = setup_root(c, home)
    contents = []
    for i in range(65):
        (root / f'outputs/{i}.png').write_bytes(b'image')
        contents.append(('tool', json.dumps({'image': str(root / f'outputs/{i}.png')}), 'image_generate'))
    # More documents than the Recent scan window, all newer than the images.
    for i in range(220):
        (root / f'outputs/{i}.md').write_text('synthetic')
        contents.append(('tool', json.dumps({'path': str(root / f'outputs/{i}.md'), 'verified': True}), 'write_file'))
    (root / 'outputs/drawing.svg').write_text('<svg/>')
    contents.append(('assistant', 'MEDIA:' + str(root / 'outputs/drawing.svg'), None))
    seed(home, root, contents)
    image_page(c)
    first = image_page(c)
    assert len(first['items']) == 50 and first['next_cursor']
    second = image_page(c, cursor=first['next_cursor'])
    ids = [r['id'] for r in first['items'] + second['items']]
    assert len(ids) == len(set(ids)) == 65 and second['next_cursor'] is None
    for change in [{}, {'media_type': ''}, {'media_type': 'video'}, {'q': 'different'},
                   {'workspace_id': wid}, {'session_id': 's'}, {'kind': 'file'}]:
        params = {'profile': 'default', 'cursor': first['next_cursor']}
        if change:
            params['media_type'] = 'image'
            params.update(change)
        response = c.get(P + '/deliverables', params=params)
        assert response.status_code == (400 if change.get('media_type') in ('', 'video') else 409)
    other = home / 'profiles/b'; other.mkdir(parents=True)
    (other / 'config.yaml').write_text('{}')
    assert c.get(P + '/deliverables', params={'profile': 'b', 'media_type': 'image',
                                            'cursor': first['next_cursor']}).status_code == 409
    mixed = c.get(P + '/deliverables', params={'profile': 'default', 'kind': 'file'}).json()
    assert len(mixed['items']) == 50 and mixed['items'][0]['display_name'] == 'drawing.svg'
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("INSERT INTO messages(id,session_id,role,content,active) VALUES(999,'s','assistant',?,1)",
                   ('MEDIA:' + str(root / 'outputs/0.png'),))
    image_page(c)  # Completes the new source pass after returning the old snapshot.
    assert c.get(P + '/deliverables', params={'profile': 'default', 'media_type': 'image',
                                            'cursor': first['next_cursor']}).status_code == 409


@pytest.mark.parametrize('tool,content', [
    ('read_file', '{"image":"$IMAGE"}'),
    ('read_file', 'MEDIA:$IMAGE'),
    ('terminal', '{"image":"$IMAGE"}'),
    ('image_generate', 'A reference MEDIA:$IMAGE in prose'),
    ('image_generate', '```text\nMEDIA:$IMAGE\n```'),
    ('image_generate', '{"success":false,"image":"$IMAGE"}'),
])
def test_generated_references_and_failed_producers_are_not_recent(client, tool, content):
    c, home = client
    root, _ = setup_root(c, home)
    image = home / 'cache/generated/images/candidate.png'
    image.parent.mkdir(parents=True); image.write_bytes(b'fixture')
    seed(home, root, [('tool', content.replace('$IMAGE', str(image)), tool)])
    image_page(c)
    assert image_page(c)['items'] == []


def test_generated_delegate_output_and_foreign_profile_do_not_leak(client):
    c, home = client
    root, _ = setup_root(c, home)
    image = home / 'cache/generated/images/candidate.png'
    image.parent.mkdir(parents=True); image.write_bytes(b'fixture')
    seed(home, root, [('tool', json.dumps({'image': str(image)}), 'image_generate')])
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('ALTER TABLE sessions ADD COLUMN source TEXT')
        db.execute('ALTER TABLE sessions ADD COLUMN model_config TEXT')
        db.execute("UPDATE sessions SET parent_session_id='parent',source='desktop',model_config=?",
                   ('{"_delegate_from":"parent"}',))
    image_page(c)
    assert image_page(c)['items'] == []
    other = home / 'profiles/b'; other.mkdir(parents=True)
    (other / 'config.yaml').write_text('{}')
    seed(other, root, [('tool', json.dumps({'image': str(image)}), 'image_generate')])
    image_page(c, profile='b')
    assert image_page(c, profile='b')['items'] == []
    own = other / 'cache/generated/images/own.png'
    own.parent.mkdir(parents=True); own.write_bytes(b'owned by b')
    with sqlite3.connect(other / 'state.db') as db:
        db.execute("INSERT INTO messages(id,session_id,role,content,tool_name,active) VALUES(999,'s','tool',?,'image_generate',1)",
                   (json.dumps({'image': str(own)}),))
    image_page(c, profile='b')
    rows = image_page(c, profile='b')['items']
    assert len(rows) == 1 and rows[0]['relative_path'] == 'own.png'
    params = {'profile': 'b', 'id': rows[0]['id']}
    assert c.get(P + '/workspace-files/download', params=params).content == b'owned by b'
    assert c.get(P + '/workspace-files/download', params={**params, 'profile': 'default'}).status_code == 404


def test_recent_refreshes_stale_unavailable_metadata_without_reindex(client):
    c, home = client
    root, _ = setup_root(c, home)
    seed(home, root, [('assistant', 'MEDIA:' + str(root / 'outputs/later.png'), None)])
    image_page(c)
    assert image_page(c)['items'] == []
    (root / 'outputs/later.png').write_bytes(b'now saved')
    row = image_page(c)['items'][0]
    assert row['availability'] == 'available'
    assert c.get(P + '/workspace-files/download', params={'profile': 'default', 'id': row['id']}).content == b'now saved'
