"""Per-profile rebuildable cache; GET reads cache, bounded background work reads DB.

A source generation fence (including WAL) invalidates old published segments.
Fingerprints include saved message bodies as well as session metadata, so edits,
rewinds and deletion do not masquerade as monotonic append-only history.
"""
import base64
import contextlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

from fastapi import HTTPException
from hermes_state_holders import read_only_db_uri
from plugins.cloudseed_mobile.deliverables import project, MAX_STRING
from plugins.cloudseed_mobile.workspace_files import digest

LOG=logging.getLogger(__name__)

MAX_SESSIONS=1000
MAX_MESSAGES=2000
MAX_SEGMENT_BYTES=4*1024*1024
REFRESH_SECONDS=0.75
_LOCKS={}
_LOCK_GUARD=threading.Lock()


def source_signature(home):
    values=[]
    for name in ('state.db','state.db-wal'):
        path=home/name
        try:
            st=path.stat(); values.append((name,st.st_ino,st.st_size,st.st_mtime_ns))
        except FileNotFoundError: values.append((name,None))
    return digest(values)


class DeliverablesIndex:
    def __init__(self,home,profile,workspaces):
        self.home=Path(home); self.profile=profile; self.workspaces=workspaces
        self.path=self.home/'plugin_data/cloudseed_mobile/deliverables.sqlite3'
        with _LOCK_GUARD:
            self.lock=_LOCKS.setdefault(str(self.path),threading.Lock())

    @contextlib.contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(self.path,timeout=1)
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS segments(sid TEXT PRIMARY KEY,fingerprint TEXT,data TEXT);
                CREATE TABLE IF NOT EXISTS entries(
                    sid TEXT, id TEXT, generation TEXT, sort_time REAL,
                    workspace_id TEXT, kind TEXT, private INTEGER, recent INTEGER,
                    search TEXT, data TEXT, inline_content TEXT,
                    PRIMARY KEY(sid,id));
                CREATE INDEX IF NOT EXISTS entries_page ON entries(generation,sort_time,id);
            ''')
            yield db
            db.commit()
        finally: db.close()

    def _meta(self,db):
        return {k:json.loads(v) for k,v in db.execute('SELECT key,value FROM meta')}

    def _save(self,db,meta):
        db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',[(k,json.dumps(v)) for k,v in meta.items()])

    def refresh(self,max_sessions=20,cancel=None):
        if not self.lock.acquire(blocking=False): return
        source=None
        try:
            deadline=time.monotonic()+REFRESH_SECONDS
            signature=source_signature(self.home)
            grant_signature=digest(self.workspaces)
            with self.connection() as cache:
                meta=self._meta(cache)
                if meta.get('source_signature')==signature and meta.get('grant_signature')==grant_signature and meta.get('refresh_status')=='ready':
                    return
                if meta.get('source_signature')!=signature or meta.get('grant_signature')!=grant_signature:
                    # Entries retain fingerprints, but only this generation is public.
                    meta={'source_signature':signature,'grant_signature':grant_signature,'generation':digest([signature,grant_signature]),'offset':0,'partial':True,'indexed_sessions':0,'total_sessions':None,'lineage':{}}
                if not (self.home/'state.db').is_file() or (self.home/'state.db').is_symlink():
                    meta.update(partial=True,refresh_status='source_missing',index_revision=digest([signature,0]),updated_at=None)
                    self._save(cache,meta); return
                try:
                    source=sqlite3.connect(read_only_db_uri(self.home/'state.db'),uri=True,timeout=0.25)
                    source.row_factory=sqlite3.Row
                    source.set_progress_handler(lambda: int(time.monotonic()>deadline or bool(cancel and cancel.is_set())),1000)
                    source.execute('BEGIN')
                    session_columns={r[1] for r in source.execute('PRAGMA table_info(sessions)')}
                    selected_columns=[c for c in ('id','cwd','parent_session_id','end_reason','archived','hidden','last_activity_at','git_metadata_generation','rewind_count') if c in session_columns]
                    if 'title' in session_columns: selected_columns.append('substr(title,1,512) AS title')
                    sessions=[dict(r) for r in source.execute('SELECT '+','.join(selected_columns)+' FROM sessions ORDER BY rowid DESC LIMIT ?', (MAX_SESSIONS+1,))]
                    total=len(sessions); overflow=total>MAX_SESSIONS; sessions=sessions[:MAX_SESSIONS]
                    ids=[s['id'] for s in sessions]
                    for table in ('entries','segments'):
                        if ids:
                            cache.execute('DELETE FROM '+table+' WHERE sid NOT IN ('+','.join('?' for _ in ids)+')',ids)
                        else: cache.execute('DELETE FROM '+table)
                    lineage={}
                    for s in sessions:
                        parent=s.get('parent_session_id')
                        lineage[s['id']]={'parent':parent,'compression':s.get('end_reason')=='compression'}
                    meta['lineage']=lineage
                    selected=sessions[meta.get('offset',0):meta.get('offset',0)+min(max_sessions,100)]
                    columns={r[1] for r in source.execute('PRAGMA table_info(messages)')}
                    available=[c for c in ('id','session_id','role','tool_call_id','tool_name','timestamp','active','message_uid','display_kind') if c in columns]
                    available += [f'substr({c},1,{MAX_STRING+1}) AS {c}' for c in ('content','tool_calls') if c in columns]
                    segment_partial=False
                    for session in selected:
                        if time.monotonic()>deadline or (cancel and cancel.is_set()): break
                        messages=[]; size=0
                        for row in source.execute('SELECT '+','.join(available)+' FROM messages WHERE session_id=? ORDER BY id LIMIT ?', (session['id'],MAX_MESSAGES+1)):
                            msg=dict(row); size+=sum(len(v) for v in msg.values() if isinstance(v,str))
                            if len(messages)>=MAX_MESSAGES or size>MAX_SEGMENT_BYTES or any(isinstance(msg.get(c),str) and len(msg[c])>MAX_STRING for c in ('content','tool_calls')):
                                segment_partial=True; break
                            messages.append(msg)
                        fingerprint=digest([grant_signature,session,messages])
                        cached=cache.execute('SELECT fingerprint FROM segments WHERE sid=?',(session['id'],)).fetchone()
                        if not cached or cached[0]!=fingerprint:
                            projection_status={}
                            try:
                                rows=[] if session.get('archived') or session.get('hidden') else project(self.profile,session,messages,self.workspaces,status=projection_status)
                            except Exception as exc:  # one malformed saved chat must never fail the whole index
                                LOG.warning('deliverables projection skipped one session: %s', type(exc).__name__)
                                rows=[]; projection_status['partial']=True
                            if len(rows)>=500 or projection_status.get('partial'): segment_partial=True
                            cache.execute('DELETE FROM entries WHERE sid=?',(session['id'],))
                            for row in rows:
                                inline=row.pop('inline_content',None)
                                recent=row['action'] in {'delivered','created','edited'} and (row['action']=='delivered' or row['kind']!='file' or row.get('relative_path','').startswith(('outputs/','docs/plans/','docs/reports/','evidence/summaries/')))
                                search=' '.join(str(row.get(k) or '') for k in ('display_name','relative_path','source_chat_title')).casefold()
                                search+=' '+next((w['name'] for w in self.workspaces if w['id']==row.get('workspace_id')),'').casefold()
                                cache.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)',(session['id'],row['id'],meta['generation'],-(row.get('observed_at') or 0),row.get('workspace_id'),row['kind'],int(row.get('private',False)),int(recent),search,json.dumps(row),inline))
                            cache.execute('INSERT OR REPLACE INTO segments VALUES(?,?,?)',(session['id'],fingerprint,'[]'))
                        else:
                            cache.execute('UPDATE entries SET generation=? WHERE sid=?',(meta['generation'],session['id']))
                        meta['offset']=meta.get('offset',0)+1
                    meta.update(total_sessions=None if overflow else total,indexed_sessions=meta.get('offset',0),partial=overflow or segment_partial or meta.get('incomplete_segments',False) or meta.get('offset',0)<len(sessions),incomplete_segments=segment_partial or meta.get('incomplete_segments',False),refresh_status='partial' if overflow or segment_partial or meta.get('offset',0)<len(sessions) else 'ready',updated_at=time.time())
                    meta['index_revision']=digest([signature,grant_signature,meta['offset']])
                except sqlite3.Error:
                    cache.execute('DELETE FROM segments')
                    cache.execute('DELETE FROM entries')
                    meta.update(partial=True,refresh_status='source_unavailable',index_revision=digest([signature,'unavailable']),updated_at=None,offset=0,indexed_sessions=0)
                self._save(cache,meta)
        finally:
            if source: source.close()
            self.lock.release()

    def snapshot(self):
        with self.connection() as db:
            meta=self._meta(db)
        current=meta.get('source_signature')==source_signature(self.home) and meta.get('grant_signature')==digest(self.workspaces)
        if not current:
            meta.update(partial=True,refresh_status='refresh_pending',generation=None)
        return meta

    def query(self,workspace_id=None,session_id=None,kind=None,q='',cursor=None,limit=50,reveal=False):
        if not 1<=limit<=200 or len(q)>256 or kind not in (None,'file','remote_media','inline_content'):
            raise HTTPException(400,'Invalid deliverables query')
        if workspace_id and workspace_id not in {w['id'] for w in self.workspaces}: raise HTTPException(404,'Workspace not found')
        meta=self.snapshot()
        lineage={session_id} if session_id else None
        if lineage:
            graph=meta.get('lineage',{})
            changed=True
            while changed:
                before=len(lineage)
                for sid,node in graph.items():
                    parent=node['parent']
                    if parent and graph.get(parent,{}).get('compression') and (sid in lineage or parent in lineage): lineage.update((sid,parent))
                changed=len(lineage)!=before
        scope=[self.profile,str(self.home),workspace_id,session_id,kind,q,reveal,meta.get('index_revision')]
        after=None
        if cursor:
            if not meta.get('generation') or len(cursor)>8192:
                raise HTTPException(409,'Invalid or stale cursor')
            try:
                data=json.loads(base64.urlsafe_b64decode(cursor))
                if data['scope']!=scope: raise ValueError()
                after=tuple(data['after'])
                if len(after)!=2 or not isinstance(after[0],(int,float)) or not isinstance(after[1],str): raise ValueError()
            except (ValueError,KeyError,TypeError): raise HTTPException(409,'Invalid or stale cursor') from None
        clauses=['generation=?']; params=[meta.get('generation')]
        if not reveal: clauses.append('private=0')
        for column,value in [('workspace_id',workspace_id),('kind',kind)]:
            if value is not None: clauses.append(column+'=?'); params.append(value)
        if lineage:
            clauses.append('sid IN ('+','.join('?' for _ in lineage)+')'); params.extend(sorted(lineage))
        else: clauses.append('recent=1')
        if q: clauses.append('instr(search,?)>0'); params.append(q.casefold())
        where=' AND '.join(clauses)
        outer='rank=1'
        if after:
            outer+=' AND (sort_time,id)>(?,?)'; params.extend(after)
        sql='WITH ranked AS (SELECT id,sort_time,data,ROW_NUMBER() OVER (PARTITION BY id ORDER BY recent DESC,sort_time,sid) rank FROM entries WHERE '+where+') SELECT id,sort_time,data FROM ranked WHERE '+outer+' ORDER BY sort_time,id LIMIT ?'
        deadline=time.monotonic()+REFRESH_SECONDS
        try:
            with self.connection() as db:
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
                fetched=list(db.execute(sql,(*params,limit+1)))
                items=[]
                for rid,sort_time,data in fetched[:limit]:
                    row=json.loads(data)
                    # Occurrence history is bounded independently of file count.
                    history={}
                    history_rows=list(db.execute('SELECT data FROM entries WHERE generation=? AND id=? ORDER BY sort_time LIMIT 21',(meta.get('generation'),rid)))
                    for (other,) in history_rows[:20]:
                        for occurrence in json.loads(other)['occurrences']:
                            if lineage and occurrence['stored_session_id'] not in lineage: continue
                            if len(history)<100: history[occurrence['id']]=occurrence
                    row['occurrences']=list(history.values())
                    row['occurrences_partial']=len(history)>=100 or len(history_rows)>20
                    items.append(row)
        except sqlite3.OperationalError as exc:
            if 'interrupt' not in str(exc): raise
            items=[]; fetched=[]; meta.update(partial=True,refresh_status='query_budget_exceeded')
        next_cursor=base64.urlsafe_b64encode(json.dumps({'scope':scope,'after':[fetched[limit-1][1],fetched[limit-1][0]]}).encode()).decode() if len(fetched)>limit else None
        return {'items':items,'next_cursor':next_cursor,'coverage':{'indexed_sessions':meta.get('indexed_sessions',0),'total_sessions':meta.get('total_sessions'),'session_cap':MAX_SESSIONS},'index_revision':meta.get('index_revision'),'updated_at':meta.get('updated_at'),'partial':meta.get('partial',True),'refresh_status':meta.get('refresh_status','cold')}

    def file_target(self,rid,reveal=False):
        meta=self.snapshot()
        with self.connection() as db:
            result=db.execute('SELECT data FROM entries WHERE generation=? AND id=? AND kind=? LIMIT 1',(meta.get('generation'),rid,'file')).fetchone()
        if not result: raise HTTPException(404,'File deliverable not found')
        row=json.loads(result[0])
        if row.get('private') and not reveal: raise HTTPException(403,'Explicit private reveal required')
        if row.get('availability')=='deleted': raise HTTPException(404,'File was deleted or moved')
        return row['workspace_id'],row['relative_path']

    def content(self,rid,reveal=False):
        meta=self.snapshot()
        with self.connection() as db:
            result=db.execute('SELECT data,inline_content FROM entries WHERE generation=? AND id=? AND kind=? AND (private=0 OR ?) LIMIT 1',(meta.get('generation'),rid,'inline_content',int(reveal))).fetchone()
        if result:
            row=json.loads(result[0])
            return {'id':rid,'profile':self.profile,'content':result[1],'version_hash':row['version_hash'],'display_type':row['display_type'],'not_saved_to_workspace':True}
        raise HTTPException(404,'Inline deliverable not found')
