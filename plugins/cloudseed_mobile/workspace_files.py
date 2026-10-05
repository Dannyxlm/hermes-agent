"""Profile grants and descriptor-relative read-only file transport (Linux/POSIX).

Never hand a pathname to FileResponse: a checked path can be replaced before
Starlette opens it. Retain the checked descriptor through streaming instead.
"""
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import unquote

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from hermes_cli.config import load_config, read_raw_config_readonly
from hermes_constants import get_hermes_home, get_default_hermes_root
from hermes_state_holders import read_only_db_uri
from plugins.cloudseed_mobile.scope import profile_id

PREVIEW_BYTES = 512 * 1024
MAX_ENTRIES = 5000
MAX_DEPTH = 6
SCAN_SECONDS = 0.25
DENIED = {'mcp-tokens','pairing','google_token.json','google_oauth_pending.json','google_oauth.json','webhook_subscriptions.json','bws_cache.json','bws_cache.enc.json','.anthropic_oauth.json','auth.lock','.hg','.svn','.cache','.next','.turbo','build','dist','target','.ssh','.aws','.azure','.gnupg','.config','.hermes','credentials','secrets','sessions','profiles','state.db','projects.db','auth.json','auth.sqlite','credential_store.json','config.yaml','.git','node_modules','__pycache__','.venv','venv'}


def check_profile(profile):
    if profile != profile_id():
        raise HTTPException(403, 'Profile not found')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def parts(path, reveal=False):
    if not isinstance(path,str) or len(path)>4096 or '\x00' in path or '\\' in path or path.startswith('/'):
        raise HTTPException(400,'Invalid relative path')
    decoded = path
    for _ in range(3):
        decoded = unquote(decoded)
    if decoded != path:
        raise HTTPException(400,'Encoded paths are not accepted')
    pp = path.split('/') if path else []
    if any(p in ('','.', '..') for p in pp):
        raise HTTPException(400,'Invalid relative path')
    low = [p.lower() for p in pp]
    if any(p in DENIED or p.startswith('.env') or p.startswith('state.db-') or p.endswith(('.pem','.key','.p12','.pfx')) or 'credential' in p or p.startswith('auth.') for p in low):
        raise HTTPException(403,'Sensitive path denied')
    if not reveal and any(low[i:i+2] in (['outputs','private'],['evidence','private']) for i in range(len(low))):
        raise HTTPException(403,'Explicit private reveal required')
    return pp


def open_absolute_directory(path):
    path = Path(path)
    if not path.is_absolute() or path == Path('/'):
        raise HTTPException(403,'Unsafe workspace root')
    fd = os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for p in path.parts[1:]:
            new = os.open(p,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd = new
        return fd
    except BaseException:
        os.close(fd); raise


def safe_root(path):
    p = Path(path).absolute()
    # Registered workspaces cannot grant the profile store or its descendants.
    root = get_default_hermes_root().absolute()
    if p == Path('/') or p == Path.home() or p == root or p.is_relative_to(root):
        return False
    try:
        parts(str(p).lstrip('/'),True)
        fd = open_absolute_directory(p); os.close(fd)
        return True
    except (OSError,HTTPException):
        return False


def workspaces(profile, include_generation=False):
    check_profile(profile)
    home = get_hermes_home()
    cfg = load_config()
    roots = {}
    path = home/'projects.db'
    if path.is_symlink(): raise HTTPException(403,'Profile store symlink denied')
    if path.is_file():
        db = sqlite3.connect(read_only_db_uri(path),uri=True,timeout=0.25)
        try:
            for row in db.execute('SELECT p.name,f.path FROM projects p JOIN project_folders f ON f.project_id=p.id WHERE p.archived=0 LIMIT 1000'):
                roots[row[1]] = row[0]
        finally:
            db.close()
    if include_generation:
        for path in cfg.get('cloudseed_mobile',{}).get('generation_roots',[])[:100]:
            roots[path]=Path(path).name
    raw=read_raw_config_readonly()
    raw_cwd=raw.get('default_cwd') or raw.get('terminal',{}).get('cwd')
    cwd=(cfg.get('default_cwd') or cfg.get('terminal',{}).get('cwd')) if raw_cwd and raw_cwd not in {'.','auto','cwd'} else None
    parent = cfg.get('cloudseed_mobile',{}).get('workspace_root')
    if not parent:
        candidates = [Path(p).parent for p in roots if Path(p).parent.name == 'hermes-workspaces']
        if cwd:
            cp = Path(cwd).expanduser()
            if cp.name == 'hermes-workspaces': candidates.append(cp)
            elif cp.parent.name == 'hermes-workspaces': candidates.append(cp.parent)
            else: roots.setdefault(str(cp),cp.name)
        parent = str(candidates[0]) if candidates else None
    if parent:
        try:
            fd = open_absolute_directory(parent)
            try:
                with os.scandir(fd) as entries:
                    for i,e in enumerate(entries):
                        if i>=1000: break
                        if e.is_dir(follow_symlinks=False):
                            roots.setdefault(str(Path(parent)/e.name),e.name)
            finally: os.close(fd)
        except (OSError,HTTPException):
            pass
    items=[]
    for path,name in roots.items():
        if not safe_root(path): continue
        root = Path(path).absolute()
        quick={}
        fd=open_absolute_directory(root)
        try:
            for key,rel in {'outputs':'outputs','plans':'docs/plans','reports':'docs/reports'}.items():
                try:
                    child=open_beneath(root,rel,True,directory=True); os.close(child); quick[key]=True
                except (OSError,HTTPException): quick[key]=False
        finally: os.close(fd)
        items.append({'id':digest([str(home.absolute()),profile,str(root)]),'name':name,'root_path':str(root),'quick_folders':quick})
    return {'items':sorted(items,key=lambda x:(x['name'],x['id'])),'partial':len(roots)>=1000}


def workspace(profile,wid,generation=False):
    for item in workspaces(profile, generation)['items']:
        if item['id']==wid: return Path(item['root_path'])
    raise HTTPException(404,'Workspace not found')


def open_beneath(root,path,reveal=False,directory=False):
    pp=parts(path,reveal)
    if not pp and not directory: raise HTTPException(400,'File path required')
    fd=open_absolute_directory(root)
    try:
        for i,p in enumerate(pp):
            flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
            if i<len(pp)-1 or directory: flags|=os.O_DIRECTORY
            new=os.open(p,flags,dir_fd=fd)
            os.close(fd); fd=new
        mode=os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise HTTPException(403,'Regular file required')
        return fd
    except BaseException:
        os.close(fd); raise


def file_open(profile,wid,path,reveal=False,generation=False):
    try:
        return open_beneath(workspace(profile,wid,generation),path,reveal)
    except FileNotFoundError: raise HTTPException(404,'File not found') from None
    except OSError: raise HTTPException(403,'File access denied') from None


def read_file(profile,wid,path,reveal=False,generation=False):
    fd=file_open(profile,wid,path,reveal,generation)
    with os.fdopen(fd,'rb') as f:
        size=os.fstat(f.fileno()).st_size
        data=f.read(PREVIEW_BYTES)
    mime=mimetypes.guess_type(path)[0] or 'application/octet-stream'
    try: text=data.decode('utf-8'); binary=b'\x00' in data
    except UnicodeDecodeError: text=data.decode('utf-8',errors='replace'); binary=True
    return {'path':path,'text':None if binary else text,'binary':binary,'truncated':size>len(data),'byteSize':size,'mime':mime}


def media_open(profile,path):
    """Original native attachments, without granting the profile/config tree."""
    check_profile(profile)
    target=Path(path)
    if not target.is_absolute(): raise HTTPException(400,'Absolute media path required')
    home=get_hermes_home().absolute()
    allowed={'.png','.jpg','.jpeg','.gif','.webp','.bmp','.svg','.mp3','.wav','.ogg','.opus','.flac','.m4a','.mp4','.mov','.webm','.pdf'}
    for name in ('images','screenshots','cache','attachments','audio_cache','image_cache',
                 'video_cache','document_cache','browser_screenshots'):
        root=home/name
        try: relative=str(target.relative_to(root))
        except ValueError: continue
        if name!='attachments' and target.suffix.lower() not in allowed:
            raise HTTPException(403,'Unsupported native media path')
        try: return open_beneath(root,relative)
        except FileNotFoundError: raise HTTPException(404,'File not found') from None
        except OSError: raise HTTPException(403,'File access denied') from None
    raise HTTPException(403,'Path outside profile media roots')


def transport(profile,wid,path,reveal=False,range_header=None,head=False,stream=False,generation=False,media=False):
    fd=media_open(profile,path) if media else file_open(profile,wid,path,reveal,generation)
    size=os.fstat(fd).st_size
    mime=mimetypes.guess_type(path)[0] or 'application/octet-stream'
    if stream and not mime.startswith(('audio/','video/')):
        os.close(fd); raise HTTPException(415,'Audio or video required')
    start,end,status=0,size-1,200
    headers={'Accept-Ranges':'bytes','X-Content-Type-Options':'nosniff','Cache-Control':'no-store'}
    if stream and range_header:
        match=re.fullmatch(r'bytes=(\d*)-(\d*)',range_header)
        try:
            if not match or not any(match.groups()): raise ValueError()
            a,b=match.groups()
            if a: start=int(a); end=min(int(b),size-1) if b else size-1
            else: start=max(0,size-int(b))
            if start>end or start>=size or size==0: raise ValueError()
        except ValueError:
            os.close(fd)
            return Response(status_code=416,headers={**headers,'Content-Range':f'bytes */{size}'})
        status=206; headers['Content-Range']=f'bytes {start}-{end}/{size}'
    length=max(0,end-start+1)
    headers['Content-Length']=str(length)
    if not stream:
        name=Path(path).name.replace('"','_').replace('\r','_').replace('\n','_')
        from urllib.parse import quote
        headers['Content-Disposition']="attachment; filename*=UTF-8''"+quote(name)
    if head:
        os.close(fd); return Response(status_code=status,headers=headers,media_type=mime)
    def chunks():
        with os.fdopen(fd,'rb') as f:
            f.seek(start); remaining=length
            while remaining:
                chunk=f.read(min(65536,remaining))
                if not chunk: break
                remaining-=len(chunk); yield chunk
    return StreamingResponse(chunks(),status_code=status,headers=headers,media_type=mime)


def listing(profile,wid,path='',q='',cursor=None,limit=50,reveal=False):
    if not 1<=limit<=200 or len(q)>256: raise HTTPException(400,'Invalid query')
    root=workspace(profile,wid)
    parts(path,reveal)
    deadline=time.monotonic()+SCAN_SECONDS
    items=[]; visited=0; partial=False
    def scan(rel,depth):
        nonlocal visited,partial
        if time.monotonic()>deadline or visited>=MAX_ENTRIES:
            partial=True; return
        try: fd=open_beneath(root,rel,reveal,directory=True)
        except FileNotFoundError: raise HTTPException(404,'Directory not found') from None
        except OSError: raise HTTPException(403,'Directory access denied') from None
        try:
            with os.scandir(fd) as entries:
                for e in entries:
                    visited+=1
                    if visited>MAX_ENTRIES or time.monotonic()>deadline:
                        partial=True; break
                    child=rel+'/'+e.name if rel else e.name
                    try: parts(child,reveal)
                    except HTTPException: continue
                    st=e.stat(follow_symlinks=False)
                    isdir=stat.S_ISDIR(st.st_mode)
                    if not (isdir or stat.S_ISREG(st.st_mode)): continue
                    if not q or q.casefold() in child.casefold():
                        items.append({'name':e.name,'path':child,'isDirectory':isdir,'byteSize':None if isdir else st.st_size,'mtime':st.st_mtime,'mime':None if isdir else mimetypes.guess_type(child)[0] or 'application/octet-stream'})
                    if q and isdir:
                        if depth<MAX_DEPTH: scan(child,depth+1)
                        else: partial=True
        finally: os.close(fd)
    scan(path,0)
    items.sort(key=lambda i:i['path'])
    revision=digest(items)
    scope=[profile,wid,path,q,reveal,revision]
    last=''
    if cursor:
        if len(cursor)>8192: raise HTTPException(409,'Invalid or stale cursor')
        try:
            data=json.loads(base64.urlsafe_b64decode(cursor))
            if data['scope']!=scope or not isinstance(data.get('last'),str): raise ValueError()
            last=data['last']
        except (ValueError,KeyError,TypeError): raise HTTPException(409,'Invalid or stale cursor') from None
    remaining=[i for i in items if i['path']>last]
    page=remaining[:limit]
    next_cursor=base64.urlsafe_b64encode(json.dumps({'scope':scope,'last':page[-1]['path']}).encode()).decode() if len(remaining)>limit else None
    return {'items':page,'next_cursor':next_cursor,'partial':partial,'workspace_id':wid,'path':path}
