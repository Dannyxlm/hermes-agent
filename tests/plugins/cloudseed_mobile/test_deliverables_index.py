"""Synthetic read-only stores and rebuildable profile cache."""
import json
import sqlite3
import pytest
from fastapi import HTTPException
from plugins.cloudseed_mobile import deliverables_index as ix
from tests.plugins.test_cloudseed_mobile import client, P
from tests.plugins.cloudseed_mobile.test_workspace_files import setup_root


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
    with pytest.raises(HTTPException): index.query(cursor=page['next_cursor'])
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
    assert response['partial'] and response['coverage']['total_sessions'] is None
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
