"""Per-profile rebuildable cache; GET reads cache, bounded background work reads DB.

A workspace grant fence invalidates access immediately. Source changes schedule a
new scan while completed session projections stay readable. Message/session
keysets checkpoint bounded work; a session replacement is published atomically.
"""
import base64
import contextlib
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
from hermes_state_holders import read_only_db_uri
from plugins.cloudseed_mobile.deliverables import project, decode, MAX_STRING
from plugins.cloudseed_mobile.workspace_files import digest

LOG=logging.getLogger(__name__)

MAX_MESSAGES=2000
MAX_SEGMENT_BYTES=4*1024*1024
REFRESH_SECONDS=0.75
# Files › Recent lists only files that exist now. Path text cut out of tool output or
# code (`+str(generation`, `example.pdf'`) never reaches it. Each page may examine this
# many candidates per requested row before handing back a cursor.
RECENT_SCAN_FACTOR=4
MAX_PUBLICATION_ROWS=2000
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
                CREATE TABLE IF NOT EXISTS session_nodes(sid TEXT PRIMARY KEY,
                    parent_id TEXT,compression INTEGER NOT NULL DEFAULT 0,scan_epoch INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS session_nodes_parent ON session_nodes(parent_id);
                CREATE INDEX IF NOT EXISTS session_nodes_scan ON session_nodes(scan_epoch,sid);
                CREATE TABLE IF NOT EXISTS entries(
                    sid TEXT, id TEXT, generation TEXT, sort_time REAL,
                    workspace_id TEXT, kind TEXT, private INTEGER, recent INTEGER,
                    search TEXT, data TEXT, inline_content TEXT,
                    PRIMARY KEY(sid,id));
                CREATE INDEX IF NOT EXISTS entries_page ON entries(generation,sort_time,id);
                CREATE TABLE IF NOT EXISTS staged(sid TEXT,id TEXT,data TEXT,inline_content TEXT,
                    PRIMARY KEY(sid,id));
                CREATE TABLE IF NOT EXISTS publication(sid TEXT PRIMARY KEY,after_id TEXT,fingerprint TEXT);
                CREATE TABLE IF NOT EXISTS prepared_entries(
                    sid TEXT,id TEXT,generation TEXT,sort_time REAL,workspace_id TEXT,kind TEXT,
                    private INTEGER,recent INTEGER,search TEXT,data TEXT,inline_content TEXT,
                    PRIMARY KEY(sid,id));
            ''')
            # Keep segments' three-column contract: the deployed runtime uses
            # positional INSERTs when a release is rolled back onto this cache.
            yield db
            db.commit()
        finally: db.close()

    def _meta(self,db):
        return {k:json.loads(v) for k,v in db.execute("SELECT key,value FROM meta WHERE key!='lineage'")}

    def _save(self,db,meta,previous):
        db.executemany('DELETE FROM meta WHERE key=?',[(k,) for k in previous.keys()-meta.keys()])
        db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',
                       [(k,json.dumps(v)) for k,v in meta.items() if k not in previous or previous[k]!=v])

    def _remember_session(self,cache,session,epoch):
        sid=session['id']; parent=session.get('parent_session_id'); compression=session.get('end_reason')=='compression'
        old=cache.execute('SELECT parent_id,compression FROM session_nodes WHERE sid=?',(sid,)).fetchone()
        cache.execute('''INSERT INTO session_nodes(sid,parent_id,compression,scan_epoch) VALUES(?,?,?,?)
                         ON CONFLICT(sid) DO UPDATE SET parent_id=excluded.parent_id,
                         compression=excluded.compression,scan_epoch=excluded.scan_epoch''',
                      (sid,parent,int(compression),epoch))
        return old is not None and old!=(parent,int(compression))

    def _lineage(self,cache,sid):
        # Both directions are indexed; UNION also terminates malformed cycles.
        return {r[0] for r in cache.execute('''WITH RECURSIVE lineage(sid) AS (
            SELECT ?
            UNION
            SELECT parent.sid FROM lineage
                JOIN session_nodes child ON child.sid=lineage.sid
                JOIN session_nodes parent ON parent.sid=child.parent_id AND parent.compression=1
            UNION
            SELECT child.sid FROM lineage
                JOIN session_nodes parent ON parent.sid=lineage.sid AND parent.compression=1
                JOIN session_nodes child ON child.parent_id=parent.sid
        ) SELECT sid FROM lineage''',(sid,))}

    def _reconcile_session(self,cache,source,selection,sid,meta):
        current=source.execute('SELECT '+selection+' FROM sessions WHERE id=?',(sid,)).fetchone()
        if current:
            # A concurrent insert/rowid move above the keyset will be projected
            # by the next source-signature scan.
            changed=self._remember_session(cache,dict(current),meta['scan_epoch'])
        else:
            cache.execute('DELETE FROM entries WHERE sid=?',(sid,))
            cache.execute('DELETE FROM segments WHERE sid=?',(sid,))
            cache.execute('DELETE FROM session_nodes WHERE sid=?',(sid,))
            changed=True
        if changed: meta['index_revision']=digest([meta.get('index_revision'),sid,time.time_ns()])
        meta['scan_cleanup']+=1

    def _stage(self,cache,sid,rows):
        if rows:
            # New messages may arrive between publication checkpoints.
            cache.execute('DELETE FROM publication WHERE sid=?',(sid,))
            cache.execute('DELETE FROM prepared_entries WHERE sid=?',(sid,))
        for row in rows:
            inline=row.pop('inline_content',None)
            old=cache.execute('SELECT data,inline_content FROM staged WHERE sid=? AND id=?',(sid,row['id'])).fetchone()
            if old:
                previous=json.loads(old[0])
                history={o['id']:o for o in previous['occurrences']}
                history.update({o['id']:o for o in row['occurrences']})
                rank=lambda r:(r['action'] in {'delivered','created','edited'},r.get('observed_at') is not None,r.get('observed_at') or 0)
                if rank(previous)>rank(row): row,inline=previous,old[1]
                row['occurrences']=list(history.values())[-100:]
                row['occurrences_partial']=previous.get('occurrences_partial',False) or len(history)>100
            cache.execute('INSERT OR REPLACE INTO staged VALUES(?,?,?,?)',(sid,row['id'],json.dumps(row),inline))

    def _publish(self,cache,sid,generation,deadline,cancel):
        """Checkpoint Python decoding/hashing; only the final SQL swap is atomic."""
        state=cache.execute('SELECT after_id,fingerprint FROM publication WHERE sid=?',(sid,)).fetchone()
        after,revision=state or (None,hashlib.sha256(generation.encode()).hexdigest())
        stopped=lambda: time.monotonic()>deadline or bool(cancel and cancel.is_set())
        clause='' if after is None else ' AND id>?'
        args=(sid,) if after is None else (sid,after)
        rows=cache.execute('SELECT id,data,inline_content FROM staged WHERE sid=?'+clause+' ORDER BY id LIMIT ?',(*args,MAX_PUBLICATION_ROWS))
        for rid,data,inline in rows:
            if stopped(): break
            row=json.loads(data)
            recent=not row.get('subagent',False) and row.get('availability') not in {'unavailable','deleted'} and row['action'] in {'delivered','created','edited'} and (row['action']=='delivered' or row['kind']!='file' or row.get('relative_path','').startswith(('outputs/','docs/plans/','docs/reports/','evidence/summaries/')))
            search=' '.join(str(row.get(k) or '') for k in ('display_name','relative_path','source_chat_title')).casefold()
            search+=' '+next((w['name'] for w in self.workspaces if w['id']==row.get('workspace_id')),'').casefold()
            cache.execute('INSERT OR REPLACE INTO prepared_entries VALUES(?,?,?,?,?,?,?,?,?,?,?)',(sid,rid,generation,-(row.get('observed_at') or 0),row.get('workspace_id'),row['kind'],int(row.get('private',False)),int(recent),search,data,inline))
            fingerprint=hashlib.sha256(revision.encode())
            fingerprint.update(data.encode()); fingerprint.update((inline or '').encode())
            revision=fingerprint.hexdigest(); after=rid
        cache.execute('INSERT OR REPLACE INTO publication VALUES(?,?,?)',(sid,after,revision))
        if stopped() or cache.execute('SELECT 1 FROM staged WHERE sid=? AND (? IS NULL OR id>?) LIMIT 1',(sid,after,after)).fetchone():
            return None
        old=cache.execute('SELECT fingerprint FROM segments WHERE sid=?',(sid,)).fetchone()
        changed=not old or old[0]!=revision
        if changed:
            # No JSON/inline-body materialization or Python loop under the final
            # swap. Readers see the previous complete projection until commit.
            cache.execute('DELETE FROM entries WHERE sid=?',(sid,))
            cache.execute('INSERT INTO entries SELECT * FROM prepared_entries WHERE sid=?',(sid,))
            cache.execute('INSERT OR REPLACE INTO segments VALUES(?,?,?)',(sid,revision,'[]'))
        cache.execute('DELETE FROM staged WHERE sid=?',(sid,))
        cache.execute('DELETE FROM publication WHERE sid=?',(sid,))
        cache.execute('DELETE FROM prepared_entries WHERE sid=?',(sid,))
        return changed

    def _messages(self,source,columns,sid,after):
        available=[c for c in ('id','session_id','role','tool_call_id','tool_name','timestamp','active','message_uid','display_kind') if c in columns]
        available += [f'substr({c},1,{MAX_STRING+1}) AS {c}' for c in ('content','tool_calls') if c in columns]
        selection=','.join(available)
        messages=[]; size=0
        # One oversized message still advances the keyset. It cannot hide all later messages.
        for row in source.execute('SELECT '+selection+' FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?', (sid,after,MAX_MESSAGES)):
            msg=dict(row)
            message_size=sum(len(v) for v in msg.values() if isinstance(v,str))
            if messages and size+message_size>MAX_SEGMENT_BYTES: break
            messages.append(msg); size+=message_size
        if not messages: return [],after
        last=messages[-1]['id']
        # A call/result pair may straddle a work unit; resolve its outcome from the
        # same read snapshot instead of publishing an incorrect pending mutation.
        result_ids={m.get('tool_call_id') for m in messages if m.get('role')=='tool'}
        calls=set()
        for msg in messages:
            raw=decode(msg.get('tool_calls') or [])
            if isinstance(raw,list):
                calls.update(c['id'] for c in raw if isinstance(c,dict) and isinstance(c.get('id'),str))
        missing=sorted(calls-result_ids)
        for offset in range(0,len(missing),500):
            batch=missing[offset:offset+500]
            rows=source.execute('SELECT '+selection+' FROM messages WHERE session_id=? AND tool_call_id IN ('+','.join('?' for _ in batch)+') AND role=? ORDER BY id',(sid,*batch,'tool'))
            messages.extend(dict(row) for row in rows)
        return messages,last

    def refresh(self,max_sessions=20,cancel=None):
        if not self.lock.acquire(blocking=False): return
        source=None
        try:
            deadline=time.monotonic()+REFRESH_SECONDS
            signature=source_signature(self.home)
            grant_signature=digest(self.workspaces)
            with self.connection() as cache:
                meta=self._meta(cache)
                previous=meta.copy()
                # Old runtimes upsert metadata without removing unknown keys.
                # Their lineage write is the signal to rebuild after rollback.
                legacy=cache.execute("SELECT 1 FROM meta WHERE key='lineage'").fetchone() is not None
                if (not legacy and meta.get('scan_version')==5 and meta.get('source_signature')==signature
                    and meta.get('grant_signature')==grant_signature and 'scan_signature' not in meta
                    and meta.get('refresh_status') in {'ready','partial'}): return
                if legacy or meta.get('grant_signature')!=grant_signature or meta.get('scan_version')!=5:
                    reconcile_legacy=legacy or meta.get('scan_version')!=5
                    generation=meta.get('generation') if meta.get('grant_signature')==grant_signature else None
                    epoch=cache.execute('SELECT COALESCE(MAX(scan_epoch),0) FROM session_nodes').fetchone()[0]
                    meta={'scan_version':5,'grant_signature':grant_signature,'generation':generation or digest([grant_signature,2]),'scan_epoch':epoch}
                    if reconcile_legacy and cache.execute('SELECT 1 FROM segments LIMIT 1').fetchone():
                        meta['legacy_after']=None
                    cache.execute('DELETE FROM staged')
                    cache.execute('DELETE FROM publication')
                    cache.execute('DELETE FROM prepared_entries')
                    cache.execute("DELETE FROM meta WHERE key='lineage'")
                if not (self.home/'state.db').is_file() or (self.home/'state.db').is_symlink():
                    meta.update(partial=True,refresh_status='source_missing',generation=None,updated_at=None)
                    self._save(cache,meta,previous); return
                meta['generation']=meta.get('generation') or digest([grant_signature,2])
                try:
                    source=sqlite3.connect(read_only_db_uri(self.home/'state.db'),uri=True,timeout=0.25)
                    source.row_factory=sqlite3.Row
                    source.set_progress_handler(lambda: int(time.monotonic()>deadline or bool(cancel and cancel.is_set())),1000)
                    source.execute('BEGIN')
                    session_columns={r[1] for r in source.execute('PRAGMA table_info(sessions)')}
                    selected_columns=['rowid AS scan_rowid']+[c for c in ('id','cwd','source','parent_session_id','end_reason','archived','hidden','git_metadata_generation','rewind_count') if c in session_columns]
                    if 'title' in session_columns: selected_columns.append('substr(title,1,512) AS title')
                    # Delegate children often inherit the parent's surface as `source`;
                    # the delegation marker Hermes writes into model_config is structural.
                    if 'model_config' in session_columns:
                        selected_columns.append("instr(coalesce(model_config,''),'\"_delegate_from\"')>0 AS delegate_child")
                    if 'scan_signature' not in meta:
                        total=source.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
                        meta.update(scan_signature=signature,scan_before=None,indexed_sessions=0,incomplete_segments=False,
                                    total_sessions=total,scan_epoch=meta['scan_epoch']+1,scan_cleanup=0)
                    budget=max(0,min(max_sessions,100))
                    before=meta.get('scan_before')
                    where='' if before is None else ' WHERE rowid<?'
                    params=() if before is None else (before,)
                    selection=','.join(selected_columns)
                    sessions=[dict(r) for r in source.execute('SELECT '+selection+' FROM sessions'+where+' ORDER BY rowid DESC LIMIT ?',(*params,budget+1))]
                    if meta.get('scan_session') and (not sessions or sessions[0]['id']!=meta['scan_session']['id']):
                        cache.execute('DELETE FROM staged')
                        cache.execute('DELETE FROM publication'); cache.execute('DELETE FROM prepared_entries')
                        meta.pop('scan_session',None); meta.pop('scan_after',None)
                    columns={r[1] for r in source.execute('PRAGMA table_info(messages)')}
                    units=0
                    for session in sessions[:budget]:
                        if time.monotonic()>deadline or (cancel and cancel.is_set()): break
                        sid=session['id']; units+=1
                        if meta.get('scan_session')!=session:
                            cache.execute('DELETE FROM staged')
                            cache.execute('DELETE FROM publication'); cache.execute('DELETE FROM prepared_entries')
                            meta.update(scan_session=session,scan_after=-1)
                        if self._remember_session(cache,session,meta['scan_epoch']):
                            meta['index_revision']=digest([meta.get('index_revision'),sid,time.time_ns()])
                        messages,last=([],meta['scan_after']) if session.get('archived') or session.get('hidden') else self._messages(source,columns,sid,meta['scan_after'])
                        status={}
                        try:
                            rows=project(self.profile,session,messages,self.workspaces,status=status,reference_limit=None)
                        except Exception as exc:
                            LOG.warning('deliverables projection skipped one message segment: %s',type(exc).__name__)
                            rows=[]; status['partial']=True
                        self._stage(cache,sid,rows)
                        meta['scan_after']=last
                        meta['incomplete_segments']=meta['incomplete_segments'] or status.get('partial',False)
                        more=bool(messages) and source.execute('SELECT 1 FROM messages WHERE session_id=? AND id>? LIMIT 1',(sid,last)).fetchone()
                        if more: break
                        published=self._publish(cache,sid,meta['generation'],deadline,cancel)
                        if published is None: break
                        meta.update(scan_before=session['scan_rowid'],indexed_sessions=meta['indexed_sessions']+1)
                        meta.pop('scan_session',None); meta.pop('scan_after',None)
                        if published: meta['index_revision']=digest([meta.get('index_revision'),sid,time.time_ns()])
                    remaining=bool(meta.get('scan_session')) or units<len(sessions)
                    if not remaining and 'legacy_after' in meta:
                        # Old segments may have no node, including deleted source
                        # sessions. Reconcile them once via a bounded cache keyset.
                        after=meta['legacy_after']
                        clause='' if after is None else ' WHERE sid>?'
                        args=() if after is None else (after,)
                        legacy_sids=list(cache.execute('SELECT sid FROM segments'+clause+' ORDER BY sid LIMIT ?',(*args,budget-units+1)))
                        checked=0
                        for (sid,) in legacy_sids[:budget-units]:
                            if time.monotonic()>deadline or (cancel and cancel.is_set()): break
                            self._reconcile_session(cache,source,selection,sid,meta)
                            meta['legacy_after']=sid
                            checked+=1; units+=1
                        remaining=checked<len(legacy_sids)
                        if not remaining: meta.pop('legacy_after')
                    if not remaining:
                        # Only old, unvisited nodes need checking. The epoch index
                        # avoids a full cache sweep, including after a v2 upgrade.
                        stale=list(cache.execute('SELECT sid FROM session_nodes WHERE scan_epoch<? ORDER BY scan_epoch,sid LIMIT ?',
                                                 (meta['scan_epoch'],budget-units+1)))
                        cleaned=0
                        for (sid,) in stale[:budget-units]:
                            if time.monotonic()>deadline or (cancel and cancel.is_set()): break
                            self._reconcile_session(cache,source,selection,sid,meta)
                            cleaned+=1
                        remaining=cleaned<len(stale)
                    if not remaining:
                        meta['source_signature']=meta.pop('scan_signature')
                    meta.update(partial=remaining or meta['incomplete_segments'],refresh_status='partial' if remaining or meta['incomplete_segments'] else 'ready',updated_at=time.time())
                except sqlite3.Error as exc:
                    # A busy writer or expired read budget must not erase published files.
                    meta.update(partial=True,refresh_status='partial' if 'interrupt' in str(exc) else 'source_unavailable')
                self._save(cache,meta,previous)
        finally:
            if source: source.close()
            self.lock.release()

    def snapshot(self):
        with self.connection() as db:
            meta=self._meta(db)
            legacy=db.execute("SELECT 1 FROM meta WHERE key='lineage'").fetchone() is not None
            publication=db.execute('SELECT sid,after_id FROM publication ORDER BY sid LIMIT 1').fetchone()
        source_available=(self.home/'state.db').is_file() and not (self.home/'state.db').is_symlink()
        grants_current=meta.get('grant_signature')==digest(self.workspaces)
        source_current=meta.get('source_signature')==source_signature(self.home)
        scan_current=not legacy and meta.get('scan_version')==5
        if not grants_current or not source_available:
            meta.update(partial=True,refresh_status='refresh_pending',generation=None)
        elif not source_current or not scan_current:
            meta.update(partial=True,refresh_status='refresh_pending')
        # Partial projection (e.g. malformed saved data) is not unfinished work.
        # Clients can stop polling it, while long sessions keep advancing even
        # before their first complete projection increments indexed_sessions.
        meta['refresh_pending']=source_available and (not grants_current or not source_current or not scan_current or 'scan_signature' in meta)
        meta['refresh_progress']=digest([meta.get('scan_signature'),meta.get('scan_before'),(meta.get('scan_session') or {}).get('id'),meta.get('scan_after'),meta.get('scan_cleanup'),publication])
        return meta

    def _exists(self,row):
        """True unless a workspace file row names a path that is not a regular file inside its
        workspace right now. Bounded to one stat; symlinks may not escape the workspace."""
        if row.get('kind')=='remote_media':
            # Unencoded quotes, brackets and backticks are not legal in a URL: such a row is
            # code cut out of a test or a log (`x.png'}])[0]['kind']`), never a delivery.
            url=row.get('url')
            if not isinstance(url,str): return False
            try: tail=urlsplit(url); tail=tail.path+tail.query+tail.fragment
            except ValueError: return False
            return not any(c in tail for c in '\'"`[]{}<>\\ ')
        if row.get('kind')!='file': return True
        root=next((w.get('root_path') for w in self.workspaces if w.get('id')==row.get('workspace_id')),None)
        relative=row.get('relative_path')
        if not root or not isinstance(relative,str) or not relative: return False
        try:
            base=Path(root).resolve(strict=True)
            target=(base/relative).resolve(strict=True)
            target.relative_to(base)
            return target.is_file()
        except (OSError,ValueError,RuntimeError):
            return False

    def query(self,workspace_id=None,session_id=None,kind=None,q='',cursor=None,limit=50,reveal=False):
        if not 1<=limit<=200 or len(q)>256 or kind not in (None,'file','remote_media','inline_content'):
            raise HTTPException(400,'Invalid deliverables query')
        if workspace_id and workspace_id not in {w['id'] for w in self.workspaces}: raise HTTPException(404,'Workspace not found')
        meta=self.snapshot()
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
        deadline=time.monotonic()+REFRESH_SECONDS
        try:
            with self.connection() as db:
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
                lineage=self._lineage(db,session_id) if session_id else None
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
                validates=not lineage
                window=limit*RECENT_SCAN_FACTOR if validates else limit
                candidates=list(db.execute(sql,(*params,window+1)))
                fetched=[]; last=None; more=len(candidates)>window
                for rid,sort_time,data in candidates[:window]:
                    if len(fetched)>=limit:
                        more=True; break
                    last=(sort_time,rid)
                    if validates and not self._exists(json.loads(data)): continue
                    fetched.append((rid,sort_time,data))
                items=[]
                histories={rid: [] for rid, _, _ in fetched[:limit]}
                if histories:
                    # Each correlated keyset stops at 21 rows; bodies outside that
                    # bound are never hydrated, even for heavily repeated files.
                    values=','.join('(?,?)' for _ in histories)
                    args=[value for ordinal,rid in enumerate(histories) for value in (rid,ordinal)]
                    history_sql='WITH requested(id,ordinal) AS (VALUES '+values+') SELECT requested.id,e.data FROM requested JOIN entries e ON e.rowid IN (SELECT rowid FROM entries WHERE generation=? AND id=requested.id ORDER BY sort_time LIMIT 21) ORDER BY requested.ordinal,e.sort_time,e.rowid'
                    for rid,data in db.execute(history_sql,(*args,meta.get('generation'))):
                        histories[rid].append((data,))
                for rid,sort_time,data in fetched[:limit]:
                    row=json.loads(data)
                    # Occurrence history is bounded independently of file count.
                    history={}
                    history_rows=histories[rid]
                    for (other,) in history_rows[:20]:
                        for occurrence in json.loads(other)['occurrences']:
                            if lineage and occurrence['stored_session_id'] not in lineage: continue
                            if len(history)<100: history[occurrence['id']]=occurrence
                    row['occurrences']=list(history.values())
                    row['occurrences_partial']=row.get('occurrences_partial',False) or len(history)>=100 or len(history_rows)>20
                    items.append(row)
        except sqlite3.OperationalError as exc:
            if 'interrupt' not in str(exc): raise
            items=[]; fetched=[]; meta.update(partial=True,refresh_status='query_budget_exceeded')
            last=None; more=False
        next_cursor=base64.urlsafe_b64encode(json.dumps({'scope':scope,'after':[last[0],last[1]]}).encode()).decode() if more and last else None
        return {'items':items,'next_cursor':next_cursor,'coverage':{'indexed_sessions':meta.get('indexed_sessions',0),'total_sessions':meta.get('total_sessions'),'session_cap':None,'refresh_pending':meta['refresh_pending'],'refresh_progress':meta['refresh_progress']},'index_revision':meta.get('index_revision'),'updated_at':meta.get('updated_at'),'partial':meta.get('partial',True),'refresh_status':meta.get('refresh_status','cold')}

    def file_target(self,rid,reveal=False):
        meta=self.snapshot()
        with self.connection() as db:
            result=db.execute('SELECT data FROM entries WHERE generation=? AND id=? AND kind=? LIMIT 1',(meta.get('generation'),rid,'file')).fetchone()
        if not result: raise HTTPException(404,'File deliverable not found')
        row=json.loads(result[0])
        if row.get('private') and not reveal: raise HTTPException(403,'Explicit private reveal required')
        if row.get('availability')=='deleted': raise HTTPException(404,'File was deleted or moved')
        if row.get('availability')=='unavailable': raise HTTPException(404,'File unavailable')
        return row['workspace_id'],row['relative_path']

    def content(self,rid,reveal=False):
        meta=self.snapshot()
        with self.connection() as db:
            result=db.execute('SELECT data,inline_content FROM entries WHERE generation=? AND id=? AND kind=? AND (private=0 OR ?) LIMIT 1',(meta.get('generation'),rid,'inline_content',int(reveal))).fetchone()
        if result:
            row=json.loads(result[0])
            return {'id':rid,'profile':self.profile,'content':result[1],'version_hash':row['version_hash'],'display_type':row['display_type'],'not_saved_to_workspace':True}
        raise HTTPException(404,'Inline deliverable not found')
