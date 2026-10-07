"""Synthetic read-only stores and rebuildable profile cache."""
import json
import sqlite3
import pytest
from fastapi import HTTPException
from plugins.cloudseed_mobile import deliverables_index as ix
from tests.plugins.test_cloudseed_mobile import client, P
from tests.plugins.cloudseed_mobile.test_workspace_files import setup_root


@pytest.fixture(autouse=True)
def _index_semantics_over_synthetic_paths(monkeypatch):
    """These tests pin index, keyset and admission semantics over synthetic paths that
    are never written to disk. Recent's existence check has its own tests in
    test_deliverables_recent_exists.py."""
    monkeypatch.setattr(ix.DeliverablesIndex, '_exists', lambda self, row: True)


def store(home,root,count=2):
    with sqlite3.connect(home/'state.db') as db:
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,cwd TEXT,parent_session_id TEXT,end_reason TEXT,archived INTEGER)')
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL,tool_calls TEXT,tool_call_id TEXT,tool_name TEXT,active INTEGER,message_uid TEXT)')
        for i in range(count):
            db.execute('INSERT INTO sessions VALUES(?,?,?,NULL,NULL,0)',(str(i),'Synthetic',str(root)))
            db.execute('INSERT INTO messages VALUES(?,?,?, ?,NULL,NULL,NULL,NULL,1,?)',(i,str(i),'assistant','MEDIA: '+str(root/f'outputs/{i}.md'),str(i)))


def test_index_keyset_source_edits_deletion_and_bounds(client):
    c,home=client; root,wid=setup_root(c,home); store(home,root,3)
    index=ix.DeliverablesIndex(home,'default',[{'id':wid,'root_path':str(root),'name':'Fixture'}])
    original=(home/'state.db').read_bytes()
    index.refresh(max_sessions=1)
    page=index.query(limit=1)
    assert page['partial'] and page['coverage']['indexed_sessions']==1
    index.refresh(max_sessions=10)
    page=index.query(limit=1)
    assert not page['partial'] and page['next_cursor']
    nextpage=index.query(limit=1,cursor=page['next_cursor'])
    assert nextpage['items'][0]['id']!=page['items'][0]['id']
    with pytest.raises(HTTPException): index.query(q='other',cursor=page['next_cursor'])
    assert (home/'state.db').read_bytes()==original
    with sqlite3.connect(home/'state.db') as db:
        db.execute("UPDATE messages SET content=? WHERE id=0",('MEDIA: '+str(root/'outputs/new.md'),))
        db.execute('DELETE FROM messages WHERE id=1')
    assert index.query(cursor=page['next_cursor'])['items']
    index.refresh(max_sessions=10)
    paths={r['relative_path'] for r in index.query()['items']}
    assert paths=={'outputs/new.md','outputs/2.md'}
    with pytest.raises(HTTPException): index.query(cursor=page['next_cursor'])
    with pytest.raises(HTTPException): index.query(limit=201)


def test_missing_corrupt_no_create_and_inline_authorization(client):
    c,home=client; root,wid=setup_root(c,home)
    index=ix.DeliverablesIndex(home,'default',[])
    index.refresh()
    assert not (home/'state.db').exists() and index.query()['partial']
    (home/'state.db').write_bytes(b'corrupt')
    index.refresh(); assert index.query()['partial']
    assert c.get(P+'/deliverables?profile=default').status_code==200
    assert c.get(P+'/deliverables/content?profile=default&id=foreign').status_code==404


def test_profile_registry_roots_secondary_folders_and_no_migration(client):
    from hermes_cli.projects_db import SCHEMA_SQL
    c,home=client
    root=home.parent/('hermes-workspaces-'+home.name); root.mkdir()
    primary=root/'Primary'; primary.mkdir(); secondary=root/'Secondary'; secondary.mkdir()
    with sqlite3.connect(home/'projects.db') as db:
        db.executescript(SCHEMA_SQL)
        db.execute('INSERT INTO projects(id,slug,name,primary_path,created_at) VALUES(?,?,?,?,0)',('p','p','Project',str(primary)))
        db.execute('INSERT INTO project_folders VALUES(?,?,?,1,0)',('p',str(primary),'Primary'))
        db.execute('INSERT INTO project_folders VALUES(?,?,?,0,0)',('p',str(secondary),'Secondary'))
    original=(home/'projects.db').read_bytes()
    response=c.get(P+'/workspaces?profile=default')
    assert {r['root_path'] for r in response.json()['items']}=={str(primary),str(secondary)}
    assert (home/'projects.db').read_bytes()==original
    b=home/'profiles/b'; b.mkdir(parents=True); (b/'config.yaml').write_text('{}')
    foreign=response.json()['items'][0]['id']
    assert c.get(P+'/workspace-files',params={'profile':'b','workspace_id':foreign}).status_code==404
    assert not (b/'projects.db').exists()
    (home/'projects.db').write_bytes(b'corrupt')
    assert c.get(P+'/workspaces?profile=default').status_code==503


def test_generation_root_and_authorized_file_id(client):
    c,home=client; root,wid=setup_root(c,home)
    generation=root.parent.parent/('Generated-'+home.name); generation.mkdir()
    (generation/'voice.mp3').write_bytes(b'original')
    (home/'config.yaml').write_text('cloudseed_mobile:\n  workspace_root: '+str(root.parent)+'\n  generation_roots:\n    - '+str(generation)+'\n')
    store(home,root,1)
    with sqlite3.connect(home/'state.db') as db:
        db.execute('UPDATE messages SET content=?',('MEDIA: '+str(generation/'voice.mp3'),))
    c.get(P+'/deliverables?profile=default')
    row=c.get(P+'/deliverables?profile=default').json()['items'][0]
    response=c.get(P+'/workspace-files/download',params={'profile':'default','id':row['id']})
    assert response.status_code==200 and response.content==b'original'
    assert c.get(P+'/workspace-files/read',params={'profile':'default','id':'foreign'}).status_code==404


def test_inline_http_authorization_and_bounded_query(client):
    c,home=client; root,wid=setup_root(c,home); store(home,root,1)
    body='<html>'+('x'*300)+'</html>'
    with sqlite3.connect(home/'state.db') as db:
        db.execute('UPDATE messages SET content=?',('```html\n'+body+'\n```',))
    assert c.get(P+'/deliverables?profile=default').json()['partial']
    listing=c.get(P+'/deliverables?profile=default').json()
    rid=listing['items'][0]['id']
    assert all('inline_content' not in row for row in listing['items'])
    assert c.get(P+'/deliverables/content',params={'profile':'default','id':rid}).json()['content']==body
    b=home/'profiles/b'; b.mkdir(parents=True); (b/'config.yaml').write_text('{}')
    assert c.get(P+'/deliverables/content',params={'profile':'b','id':rid}).status_code==404
    for query in ['limit=201','q='+('x'*257),'kind=unknown']:
        assert c.get(P+'/deliverables?profile=default&'+query).status_code==400


def test_metadata_query_does_not_hydrate_inline_bodies(client):
    import tracemalloc
    import time
    c,home=client; root,wid=setup_root(c,home); store(home,root,40)
    with sqlite3.connect(home/'state.db') as db:
        db.execute('UPDATE messages SET content=?',('```html\n<html>'+('x'*300000)+'</html>\n```',))
    index=ix.DeliverablesIndex(home,'default',[{'id':wid,'root_path':str(root),'name':'Fixture'}])
    for _ in range(10):
        index.refresh(max_sessions=100)
        if not index.query(limit=1)['partial']: break
    tracemalloc.start(); start=time.perf_counter()
    result=index.query(limit=1)
    elapsed=time.perf_counter()-start
    _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
    print(json.dumps({'benchmark':'40_sessions_300k_inline_each','query_seconds':elapsed,'peak_python_bytes':peak,'indexed_sessions':result['coverage']['indexed_sessions']}))
    assert elapsed<2
    assert len(result['items'])==1
    assert peak<8*1024*1024


def test_cancel_refresh_preserves_partial(client):
    import threading
    c,home=client; root,wid=setup_root(c,home); store(home,root,40)
    event=threading.Event(); event.set()
    index=ix.DeliverablesIndex(home,'default',[{'id':wid,'root_path':str(root),'name':'Fixture'}])
    index.refresh(cancel=event)
    assert index.query()['partial']
    assert index.query()['coverage']['indexed_sessions']==0


def test_cold_coverage_never_invents_total_or_imports_private(client):
    c,home=client; root,wid=setup_root(c,home); store(home,root,1005)
    with sqlite3.connect(home/'state.db') as db:
        db.execute('UPDATE messages SET content=? WHERE session_id=?',('MEDIA: '+str(root/'outputs/private/report.md'),'1004'))
    index=ix.DeliverablesIndex(home,'default',[{'id':wid,'root_path':str(root),'name':'Fixture'}]); index.refresh(max_sessions=1)
    response=index.query()
    assert response['partial'] and response['coverage']['total_sessions'] == 1005
    assert response['items']==[]
    assert index.query(reveal=True)['items'][0]['private']


def test_source_store_symlink_never_crosses_profile(client):
    c,home=client; root,wid=setup_root(c,home)
    b=home/'profiles/b'; b.mkdir(parents=True); (b/'config.yaml').write_text('{}')
    store(b,root,1)
    (home/'state.db').symlink_to(b/'state.db')
    c.get(P+'/deliverables?profile=default')
    response=c.get(P+'/deliverables?profile=default').json()
    assert response['partial'] and response['items']==[]
    (home/'projects.db').symlink_to(b/'state.db')
    assert c.get(P+'/workspaces?profile=default').status_code in (403,503)


def test_repeated_cross_chat_history_is_honestly_bounded(client):
    c,home=client; root,wid=setup_root(c,home); store(home,root,25)
    with sqlite3.connect(home/'state.db') as db:
        db.execute('UPDATE messages SET content=?',('MEDIA: '+str(root/'outputs/shared.md'),))
    index=ix.DeliverablesIndex(home,'default',[{'id':wid,'root_path':str(root),'name':'Fixture'}]); index.refresh(max_sessions=100)
    row=index.query()['items'][0]
    assert row['occurrences_partial'] and len(row['occurrences'])<=100


@pytest.mark.parametrize('limit', [1, 10, 50])
def test_occurrence_page_uses_one_bounded_query(client, monkeypatch, limit):
    c, home = client
    root, wid = setup_root(c, home)
    store(home, root, 25)
    with sqlite3.connect(home / 'state.db') as db:
        body = '\n'.join('MEDIA: ' + str(root / f'outputs/shared-{i}.md') for i in range(50))
        db.execute('UPDATE messages SET content=?', (body,))
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    for _ in range(10):
        index.refresh(max_sessions=100)
        if not index.snapshot()['refresh_pending']: break
    expected = index.query(limit=limit)
    # Today's per-file query remains an independent ordering/bounds oracle.
    with index.connection() as db:
        generation = index._meta(db)['generation']
        for row in expected['items']:
            history = {}
            history_rows = list(db.execute('SELECT data FROM entries WHERE generation=? AND id=? ORDER BY sort_time LIMIT 21', (generation, row['id'])))
            for (data,) in history_rows[:20]:
                for occurrence in json.loads(data)['occurrences']:
                    if len(history) < 100: history[occurrence['id']] = occurrence
            row['occurrences'] = list(history.values())
            row['occurrences_partial'] = row.get('occurrences_partial', False) or len(history) >= 100 or len(history_rows) > 20
    statements = []
    real = sqlite3.connect
    def connect(*args, **kwargs):
        db = real(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(ix.sqlite3, 'connect', connect)
    assert json.dumps(index.query(limit=limit)).encode() == json.dumps(expected).encode()
    page_statement_count = len(statements)
    history_queries = [sql for sql in statements if 'FROM entries' in sql and 'sort_time' in sql and 'recent DESC' not in sql]
    assert len(history_queries) == 1
    statements.clear()
    index.query(limit=1)
    assert len(statements) == page_statement_count
    assert len(expected['items']) == limit
    assert all(len(row['occurrences']) == 20 and row['occurrences_partial'] for row in expected['items'])


def test_http_contract_examples(client):
    c,home=client; root,wid=setup_root(c,home); store(home,root,2)
    (root/'outputs/report.md').write_text('# Synthetic\n')
    (root/'clip.mp4').write_bytes(b'0123456789')
    inline='<html>'+('x'*200)+'</html>'
    with sqlite3.connect(home/'state.db') as db:
        db.execute('UPDATE messages SET content=? WHERE id=0',('MEDIA: '+str(root/'outputs/report.md'),))
        db.execute('UPDATE messages SET content=? WHERE id=1',('```html\n'+inline+'\n```',))
    c.get(P+'/deliverables?profile=default')
    examples=[]
    def capture(method,route,params,headers=None):
        r=c.request(method,P+route,params=params,headers=headers)
        body=r.json() if r.headers.get('content-type','').startswith('application/json') else r.content.decode()
        examples.append({'method':method,'route':route,'query':params,'request_headers':headers or {},'status':r.status_code,'headers':{k:v for k,v in r.headers.items() if k in ('content-type','content-length','content-range','accept-ranges','content-disposition','cache-control')},'body':body})
        return r
    assert capture('GET','/workspaces',{'profile':'default'}).status_code==200
    rows=capture('GET','/deliverables',{'profile':'default'}).json()['items']
    inline_id=next(row['id'] for row in rows if row['kind']=='inline_content')
    assert capture('GET','/deliverables/content',{'profile':'default','id':inline_id}).json()['content']==inline
    assert capture('GET','/workspace-files',{'profile':'default','workspace_id':wid,'path':'outputs'}).status_code==200
    params={'profile':'default','workspace_id':wid,'path':'outputs/report.md'}
    assert capture('GET','/workspace-files/read',params).json()['text']=='# Synthetic\n'
    assert capture('GET','/workspace-files/download',params).content==b'# Synthetic\n'
    params={**params,'path':'clip.mp4'}
    assert capture('GET','/workspace-files/stream',params,{'Range':'bytes=2-5'}).content==b'2345'
    assert capture('HEAD','/workspace-files/stream',params).content==b''
    assert capture('GET','/workspace-files/stream',params,{'Range':'bytes=20-'}).status_code==416
    assert capture('GET','/workspace-files/read',{'profile':'default','workspace_id':wid,'path':'.ENV','reveal':1}).status_code==403
    assert capture('GET','/deliverables',{'profile':'default','limit':201}).status_code==400
    assert capture('GET','/deliverables',{'profile':'default','cursor':'invalid'}).status_code==409
    assert capture('GET','/workspace-files',{'profile':'default','workspace_id':'unknown'}).status_code==404
    assert capture('GET','/workspace-files/stream',{'profile':'default','workspace_id':wid,'path':'outputs/report.md'}).status_code==415
    assert capture('GET','/workspaces',{}).status_code==422
    c.headers.pop('Authorization')
    assert capture('GET','/workspaces',{'profile':'default'}).status_code==401
    print('CONTRACT_EXAMPLES='+json.dumps(examples))


def test_native_lineage_and_profile_fences(client):
    c,home=client; root,wid=setup_root(c,home); store(home,root)
    with sqlite3.connect(home/'state.db') as db:
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id='0'")
        db.execute("UPDATE sessions SET parent_session_id='0' WHERE id='1'")
    index=ix.DeliverablesIndex(home,'default',[{'id':wid,'root_path':str(root),'name':'Fixture'}]); index.refresh()
    assert len(index.query(session_id='1')['items'])==2
    b=home/'profiles/b'; b.mkdir(parents=True); (b/'config.yaml').write_text('{}')
    response=c.get(P+'/deliverables?profile=b')
    assert response.status_code==200 and response.json()['items']==[]
    assert not (b/'state.db').exists()
    assert c.get(P+'/deliverables?profile=missing').status_code==404
    c.headers.pop('Authorization'); assert c.get(P+'/deliverables?profile=default').status_code==401


def test_one_malformed_session_marks_partial_instead_of_failing_the_index(client, monkeypatch):
    c,home=client; root,wid=setup_root(c,home); store(home,root,3)
    real=ix.project
    def flaky(profile,session,*a,**k):
        if session['id']=='1': raise TypeError("cannot use 'dict' as a set element")
        return real(profile,session,*a,**k)
    monkeypatch.setattr(ix,'project',flaky)
    index=ix.DeliverablesIndex(home,'default',[{'id':wid,'root_path':str(root),'name':'Fixture'}])
    index.refresh(max_sessions=10)
    page=index.query()
    assert {r['relative_path'] for r in page['items']}=={'outputs/0.md','outputs/2.md'}
    assert page['partial'] is True


def test_refresh_continues_past_session_message_and_byte_budgets(client, monkeypatch):
    c, home = client
    root, wid = setup_root(c, home)
    store(home, root, 1005)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('DELETE FROM messages')
        db.executemany('INSERT INTO messages(id,session_id,role,content,active) VALUES(?,?,?,?,1)',
                       [(i, '0', 'assistant', 'x' * 4096) for i in range(2001)])
        call=[{'id':'across-batches','function':{'name':'write_file','arguments':{'path':'outputs/paired.md'}}}]
        db.execute('UPDATE messages SET tool_calls=? WHERE id=31',(json.dumps(call),))
        db.execute('UPDATE messages SET content=? WHERE id=33',('x'*(1024*1024+1),))
        db.execute('INSERT INTO messages(id,session_id,role,content,tool_call_id,active) VALUES(2002,?,?,?,?,1)',('0','tool','{\"success\":true}','across-batches'))
        db.execute('INSERT INTO messages(id,session_id,role,content,active) VALUES(2001,?,?,?,1)',
                   ('0', 'assistant', '\n'.join('MEDIA: ' + str(root / f'outputs/late-{i}.md') for i in range(550))))
    # Small work units force continuation even on the fast fixture database.
    monkeypatch.setattr(ix, 'MAX_MESSAGES', 200)
    monkeypatch.setattr(ix, 'MAX_SEGMENT_BYTES', 128 * 1024)
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    for _ in range(150):
        index.refresh(max_sessions=100)
        page = index.query()
        if not page['partial']:
            break
    assert not page['partial']
    assert page['coverage']['indexed_sessions'] == 1005
    paths=set()
    page=index.query(limit=200)
    while True:
        paths.update(row['relative_path'] for row in page['items'])
        if not page['next_cursor']: break
        page=index.query(limit=200,cursor=page['next_cursor'])
    assert paths == {f'outputs/late-{i}.md' for i in range(550)} | {'outputs/paired.md'}
    paired=index.query(q='paired')['items'][0]
    assert paired['action']=='created'
    assert {item['action'] for item in paired['occurrences']}=={'created'}


def test_published_links_survive_unrelated_writes_and_refresh_publishes_atomically(client, monkeypatch):
    c, home = client
    root, wid = setup_root(c, home)
    store(home, root, 2)
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    index.refresh()
    before = index.query()
    row = next(item for item in before['items'] if item['relative_path'] == 'outputs/0.md')
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('INSERT INTO messages(id,session_id,role,content,active) VALUES(2,?,?,?,1)',
                   ('1', 'user', 'unrelated message'))
    assert index.file_target(row['id']) == (wid, 'outputs/0.md')
    assert len(index.query()['items']) == len(before['items'])
    # Existing rows stay published until a complete replacement for that session exists.
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('UPDATE messages SET content=? WHERE id=0', ('MEDIA: ' + str(root / 'outputs/new.md'),))
        db.executemany('INSERT INTO messages(id,session_id,role,content,active) VALUES(?,?,?,?,1)',
                       [(i, '0', 'assistant', 'no file') for i in range(3, 30)])
    monkeypatch.setattr(ix, 'MAX_MESSAGES', 3)
    index.refresh(max_sessions=1)
    index.refresh(max_sessions=1)
    assert index.file_target(row['id']) == (wid, 'outputs/0.md')
    assert all(item['relative_path'] != 'outputs/new.md' for item in index.query()['items'])
    for step in range(20):
        if step<10:
            with sqlite3.connect(home / 'state.db') as db:
                db.execute('INSERT INTO messages(id,session_id,role,content,active) VALUES(?,?,?,?,1)',(100+step,'1','user','ongoing writes'))
        index.refresh(max_sessions=1)
        if not index.query()['partial']:
            break
    assert {item['relative_path'] for item in index.query()['items']} == {'outputs/new.md', 'outputs/1.md'}
    with pytest.raises(HTTPException):
        index.file_target(row['id'])


def test_refresh_progress_advances_within_long_session_and_ends_despite_partial_projection(client):
    c, home = client
    root, wid = setup_root(c, home)
    store(home, root, 1)
    with sqlite3.connect(home / 'state.db') as db:
        db.executemany('INSERT INTO messages(id,session_id,role,content,active) VALUES(?,?,?,?,1)',
                       [(i, '0', 'assistant', 'no file') for i in range(1, 2050)])
        # This malformed saved tool call is a permanent projection limitation,
        # independent of the remaining message batches.
        db.execute('UPDATE messages SET tool_calls=? WHERE id=1500', ('[{"function":1}]',))
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    cold = index.query()['coverage']
    assert cold['refresh_pending'] is True
    index.refresh(max_sessions=1)
    middle = index.query()['coverage']
    assert middle['refresh_pending'] is True
    assert middle['refresh_progress'] != cold['refresh_progress']
    assert middle['indexed_sessions'] == 0
    index.refresh(max_sessions=1)
    final = index.query()
    assert final['partial'] is True
    assert final['coverage']['refresh_pending'] is False
    assert final['coverage']['refresh_progress'] != middle['refresh_progress']
    assert final['coverage']['indexed_sessions'] == 1
    index.refresh(max_sessions=1)
    assert index.query()['coverage']['refresh_progress'] == final['coverage']['refresh_progress']


def test_large_store_refresh_bounds_scanned_work_and_metadata_writes(client, monkeypatch):
    c, home = client
    root, wid = setup_root(c, home)
    store(home, root, 0)
    with sqlite3.connect(home / 'state.db') as db:
        db.executemany('INSERT INTO sessions(id,title,cwd) VALUES(?,?,?)',
                       [(str(i), 'Synthetic', str(root)) for i in range(10000)])
    source_steps = []; source_queries = []
    real_connect = sqlite3.connect

    class MeasuredConnection(sqlite3.Connection):
        def set_progress_handler(self, callback, instructions):
            source_steps.append(0)
            slot = len(source_steps) - 1

            def measure():
                source_steps[slot] += instructions
                return callback()

            super().set_progress_handler(measure, instructions)

    def connect(database, *args, **kwargs):
        if kwargs.get('uri'):
            kwargs['factory'] = MeasuredConnection
        db = real_connect(database, *args, **kwargs)
        if kwargs.get('uri'): db.set_trace_callback(source_queries.append)
        return db

    monkeypatch.setattr(ix.sqlite3, 'connect', connect)
    index = ix.DeliverablesIndex(home, 'default', [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}])
    index.refresh(max_sessions=1)
    with index.connection() as db:
        db.executescript('''
            CREATE TABLE meta_writes(key TEXT);
            CREATE TRIGGER meta_insert AFTER INSERT ON meta BEGIN INSERT INTO meta_writes VALUES(NEW.key); END;
            CREATE TRIGGER meta_update AFTER UPDATE ON meta BEGIN INSERT INTO meta_writes VALUES(NEW.key); END;
            CREATE TRIGGER meta_delete AFTER DELETE ON meta BEGIN INSERT INTO meta_writes VALUES(OLD.key); END;
        ''')
    index.refresh(max_sessions=1)
    index.refresh(max_sessions=1)
    # VM work is deterministic: scanning the full store cannot hide behind fast hardware.
    assert len(source_steps) == 3
    assert max(source_steps) < 5000
    assert sum('COUNT(' in sql.upper() for sql in source_queries) <= 1
    with index.connection() as db:
        meta = index._meta(db)
        written = {row[0] for row in db.execute('SELECT key FROM meta_writes')}
    assert meta['indexed_sessions'] == 3
    assert meta['scan_before'] == 9998
    assert meta['total_sessions'] == 10000
    assert len(json.dumps(meta)) < 4096
    assert not written.intersection({'generation', 'grant_signature', 'scan_signature', 'scan_version', 'total_sessions'})


def test_legacy_cache_rebuilds_lineage_and_reconciles_deletion_in_bounded_units(client):
    c, home = client
    root, wid = setup_root(c, home)
    store(home, root, 6)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('ALTER TABLE sessions ADD COLUMN hidden INTEGER DEFAULT 0')
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id='0'")
        db.execute("UPDATE sessions SET parent_session_id='0' WHERE id='1'")
        db.execute("UPDATE sessions SET parent_session_id='1' WHERE id='2'")
    workspaces = [{'id': wid, 'root_path': str(root), 'name': 'Fixture'}]
    index = ix.DeliverablesIndex(home, 'default', workspaces)
    index.refresh()
    original = index.query()
    original_ids = {row['id'] for row in original['items']}
    # A frozen v2 cache fixture retains published rows but has only JSON lineage.
    with sqlite3.connect(index.path) as db:
        db.executescript('''
            ALTER TABLE segments RENAME TO current_segments;
            CREATE TABLE segments(sid TEXT PRIMARY KEY,fingerprint TEXT,data TEXT);
            INSERT INTO segments SELECT sid,fingerprint,data FROM current_segments;
            DROP TABLE current_segments;
            DROP TABLE session_nodes;
        ''')
        db.execute("UPDATE meta SET value='2' WHERE key='scan_version'")
        db.execute('INSERT INTO meta VALUES(?,?)', ('lineage', json.dumps({'0': {'parent': None, 'compression': True}})))
    index = ix.DeliverablesIndex(home, 'default', workspaces)
    before = index.query()
    assert before['coverage']['refresh_pending']
    assert {row['id'] for row in before['items']} == original_ids
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("UPDATE sessions SET archived=1 WHERE id='4'")
        db.execute("UPDATE sessions SET hidden=1 WHERE id='3'")
        db.execute("DELETE FROM sessions WHERE id='5'")
    for _ in range(5):
        index.refresh(max_sessions=1)
    page = index.query()
    assert page['coverage']['indexed_sessions'] == 5
    assert page['coverage']['refresh_pending']
    assert {row['relative_path'] for row in page['items']} == {'outputs/0.md', 'outputs/1.md', 'outputs/2.md', 'outputs/5.md'}
    # Deletion reconciliation is its own bounded, visible checkpoint.
    progress = page['coverage']['refresh_progress']
    for _ in range(6):
        index.refresh(max_sessions=1)
        next_page = index.query()
        assert next_page['coverage']['refresh_progress'] != progress
        progress = next_page['coverage']['refresh_progress']
        if not next_page['coverage']['refresh_pending']: break
    page = index.query()
    assert not page['coverage']['refresh_pending']
    assert {row['relative_path'] for row in page['items']} == {'outputs/0.md', 'outputs/1.md', 'outputs/2.md'}
    assert {row['relative_path'] for row in index.query(session_id='1')['items']} == {'outputs/0.md', 'outputs/1.md'}
    assert {row['relative_path'] for row in index.query(session_id='2')['items']} == {'outputs/2.md'}
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT 1 FROM meta WHERE key='lineage'").fetchone() is None
        assert db.execute("SELECT 1 FROM segments WHERE sid='5'").fetchone() is None
        # Deployed e17897e0 writes all three positional values on rollback.
        # Exercise its SQL contract, including an existing row and a fresh row.
        fingerprint, data = db.execute("SELECT fingerprint,data FROM segments WHERE sid='0'").fetchone()
        db.execute('INSERT OR REPLACE INTO segments VALUES(?,?,?)', ('0', fingerprint, data))
        db.execute('INSERT OR REPLACE INTO segments VALUES(?,?,?)', ('rollback-created', 'old-runtime', '[]'))
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('lineage', '{}'))
        assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
    assert index.query()['coverage']['refresh_pending']
    # Source deletion after an earlier batch was visited forces a later complete
    # pass; it must not reset the active keyset and starve the oldest sessions.
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("UPDATE sessions SET title='changed' WHERE id='4'")
    index.refresh(max_sessions=1)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("DELETE FROM sessions WHERE id='4'")
    for _ in range(20):
        index.refresh(max_sessions=1)
        if not index.query()['coverage']['refresh_pending']: break
    assert not index.query()['coverage']['refresh_pending']
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT 1 FROM segments WHERE sid='4'").fetchone() is None
        assert db.execute("SELECT 1 FROM segments WHERE sid='rollback-created'").fetchone() is None
        assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
    revoked = ix.DeliverablesIndex(home, 'default', [])
    assert revoked.query()['items'] == []


def test_publication_checkpoint_cancellation_keeps_previous_complete_rows(client, monkeypatch):
    import threading
    c,home=client; root,wid=setup_root(c,home); store(home,root,1)
    index=ix.DeliverablesIndex(home,'default',[{'id':wid,'root_path':str(root),'name':'Fixture'}])
    index.refresh()
    old=index.query()['items'][0]
    with sqlite3.connect(home/'state.db') as db:
        db.execute('UPDATE messages SET content=?',('\n'.join('MEDIA: '+str(root/f'outputs/new-{n}.md') for n in range(7)),))
    monkeypatch.setattr(ix,'MAX_PUBLICATION_ROWS',2)
    index.refresh()
    first=index.snapshot()['refresh_progress']
    assert index.query()['items']==[old]
    cancel=threading.Event(); cancel.set()
    index.refresh(cancel=cancel)
    assert index.query()['items']==[old]
    assert index.snapshot()['refresh_progress']==first
    index.refresh()
    assert index.query()['items']==[old]
    assert index.snapshot()['refresh_progress']!=first
    for _ in range(10):
        index.refresh()
        if not index.snapshot()['refresh_pending']: break
    rows=index.query()['items']
    assert {r['relative_path'] for r in rows}=={f'outputs/new-{n}.md' for n in range(7)}
    assert not index.snapshot()['refresh_pending']
    with index.connection() as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert db.execute('SELECT count(*) FROM prepared_entries').fetchone()[0]==0
