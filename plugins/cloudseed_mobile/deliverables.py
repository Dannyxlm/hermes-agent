"""Bounded message-derived references. Arguments are intent, never success.

This is a rebuildable read model, not a file revision or canonical transcript.
Only saved assistant content and recognized tool output is inspected.
"""
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit
from fastapi import HTTPException
from plugins.cloudseed_mobile.workspace_files import digest, open_beneath, parts, workspace_aliases

SCHEMA_VERSION=1
MAX_STRING=1024*1024
MAX_NODES=512
MAX_REFERENCES=500
MUTATIONS={'write_file':'created','write':'created','patch':'edited','edit':'edited','edit_file':'edited','move':'edited','move_file':'edited','delete':'edited','delete_file':'edited','read_file':'read'}
PRODUCERS={'image_generate','text_to_speech','write_file','pdf','powerpoint','docx','xlsx','browser_vision','capture_screenshot'}
OUTPUT_KEYS={'path','file_path','output_path','image_url','audio_path','video_path','download_url','media','url'}
# These tools echo files, transcripts, page text or arbitrary program stdout.
# A MEDIA example in that content is not a delivery by the enclosing tool.
CONTENT_READERS={
    'read_file','search_files','terminal','process','process_manage','execute_code',
    'web_extract','web_search','session_search','skill_view','skills_list',
    'skill_manage','read_terminal','read_window_below','delegate_task',
    'browser_exec','browser_cdp','browser_snapshot','browser_navigate',
    'browser_console','browser_click','browser_back','browser_scroll',
    'browser_press','browser_type','browser_get_images',
}


def decode(value):
    if isinstance(value,str) and len(value)<=MAX_STRING:
        try: return json.loads(value)
        except (ValueError,RecursionError): return value
    return value


def unwrap(value):
    value=decode(value)
    for _ in range(6):
        if isinstance(value,dict) and value.get('type')=='untrusted_tool_result':
            value=decode(value.get('content'))
        else: break
    return value


def visible(text):
    return re.sub(r'<(think|thinking|reasoning|scratchpad)\b[^>]*>.*?(?:</\1>|$)','',text,flags=re.S|re.I)


def strings(value,status=None):
    """Bounded scan of one message. Depth/node/length cut-offs set status['truncated'] (a
    per-message scan detail), not 'partial', which means unindexed coverage the UI must flag."""
    status={} if status is None else status
    stack=[(decode(value),0,None)]; visited=0
    while stack and visited<MAX_NODES:
        value,depth,key=stack.pop(); visited+=1
        if depth>6:
            status['truncated']=True; continue
        if isinstance(value,dict):
            kind=value.get('type')
            if isinstance(kind,str) and kind in {'reasoning','thinking','scratchpad'}: continue
            if len(value)>MAX_NODES: status['truncated']=True
            stack.extend((v,depth+1,k) for k,v in list(value.items())[:MAX_NODES] if k not in {'reasoning','reasoning_content','scratchpad'})
        elif isinstance(value,list):
            if len(value)>MAX_NODES: status['truncated']=True
            stack.extend((v,depth+1,key) for v in value[:MAX_NODES])
        elif isinstance(value,str) and len(value)<=MAX_STRING:
            parsed=decode(value)
            if isinstance(parsed,(dict,list)): stack.append((parsed,depth+1,key))
            else: yield key,value
        elif isinstance(value,str): status['truncated']=True
    if stack: status['truncated']=True


MEDIA_DELIVERY_EXTS = 'png jpg jpeg gif webp bmp tiff svg mp4 mov avi mkv webm 3gp mp3 m2a wav ogg opus m4a flac pdf docx doc odt rtf txt md epub xlsx xls ods csv tsv json xml yaml yml kmz kml geojson gpx pptx ppt odp key zip tar gz tgz bz2 xz 7z rar apk ipa html htm'.split()
_MEDIA_EXTS = '|'.join(sorted(MEDIA_DELIVERY_EXTS, key=len, reverse=True))
_MEDIA_ANCHORED = rf"(?:~/|/|[A-Za-z]:[/\\])\S+?(?:[^\S\n]+\S+?)*?\.(?:{_MEDIA_EXTS})(?=[\s`\"'*_,;:)\]}}]|MEDIA:|$)"
_MEDIA_TAG = re.compile(r'MEDIA:[ \t]*(?P<path>`[^`\n]+`|"[^"\n]+"|\'[^\'\n]+\'|' + _MEDIA_ANCHORED + r'|[^\s`"]+)')


def plausible_media_path(value):
    return '/' in value or '\\' in value or re.search(r'\.[^.]', value) is not None


def media_candidates(text):
    """Desktop's quoted, extension-anchored, then whitespace-bounded grammar."""
    for match in _MEDIA_TAG.finditer(text):
        value = match.group('path').strip()
        quoted = value[0] in '\"\'`' and value[-1] == value[0]
        if quoted:
            value = value[1:-1]
        else:
            value = value.rstrip('`"')
            while value and value[-1] in '.,;:!?' and plausible_media_path(value[:-1]):
                value = value[:-1]
        if plausible_media_path(value):
            yield match, value


def references(text, assistant=False):
    if not isinstance(text,str): return
    text=text[:MAX_STRING]
    # Fenced examples remain searchable references, never assistant deliveries.
    fenced = set(); fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^[ \t]{0,3}(`{3,}|~{3,})', line)
        if fence:
            fenced.add(offset)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        elif marker:
            fence = marker[1]; fenced.add(offset)
        offset += len(line)
    code_spans = iter(re.finditer(r'(?<!`)(`+)(?!`)(.*?)\1(?!`)', text, re.S))
    span = next(code_spans, None)
    for match, value in media_candidates(text):
        while span is not None and span.end() <= match.start():
            span = next(code_spans, None)
        in_code = span is not None and span.start() <= match.start() < span.end()
        start = text.rfind('\n', 0, match.start()) + 1
        end = text.find('\n', match.end())
        if end < 0: end = len(text)
        standalone = not text[start:match.start()].strip() and not text[match.end():end].strip()
        action = 'delivered' if not assistant or (standalone and start not in fenced and not in_code) else 'referenced'
        yield value, action
    for m in re.finditer(r'!?\[[^\]]*\]\(([^)\n]+)\)',text):
        value=m.group(1).strip().strip('<>')
        if not value.startswith(('http:','https:')): yield value,'referenced'


def locator(value,session,workspaces,aliases=None):
    if not isinstance(value,str) or len(value)>4096: return None
    if value.startswith('https://'):
        try:
            u=urlsplit(value); host=u.hostname
            if not host or u.username or u.password or host.lower()=='localhost' or host.lower().endswith(('.localhost','.local')): return None
            try:
                if not ipaddress.ip_address(host).is_global: return None
            except ValueError: pass
        except ValueError: return None
        # No server fetch/proxy. Query is retained for explicit owner open only.
        return {'kind':'remote_media','url':value,'workspace_id':None,'relative_path':None,'display_name':PurePosixPath(u.path).name or host}
    if '://' in value or '\x00' in value: return None
    path=PurePosixPath(value)
    if not path.is_absolute():
        cwd=session.get('cwd')
        if not cwd: return None
        path=PurePosixPath(cwd)/path
    aliases = workspace_aliases(workspaces) if aliases is None else aliases
    for old, current in aliases.items():
        try: relative = path.relative_to(PurePosixPath(old))
        except ValueError: continue
        path = PurePosixPath(current) / relative
        break
    for ws in sorted(workspaces,key=lambda w:len(w['root_path']),reverse=True):
        try: rel=str(path.relative_to(PurePosixPath(ws['root_path'])))
        except ValueError: continue
        try: parts(rel,True)
        except HTTPException: return None
        return {'kind':'file','workspace_id':ws['id'],'relative_path':rel,'display_name':path.name}
    return None


def project(profile,session,messages,workspaces,status=None,reference_limit=MAX_REFERENCES):
    status={} if status is None else status
    rows={}; results={m.get('tool_call_id'):m for m in messages if m.get('role')=='tool' and isinstance(m.get('tool_call_id'),str) and m.get('tool_call_id') and m.get('active',1)!=0}
    seen=set(); count=0; ranks={}
    aliases=workspace_aliases(workspaces)
    roots={w['id']:w['root_path'] for w in workspaces}
    availability={}
    subagent=bool(session.get('parent_session_id') and (session.get('source') in {'subagent','delegate'} or session.get('delegate_child')))
    message_order={id(m):i for i,m in enumerate(messages)}
    def add(value,action,message,provenance,inline=None):
        nonlocal count
        if reference_limit is not None and count>=reference_limit:
            status['partial']=True; return
        loc=locator(value,session,workspaces,aliases) if inline is None else {'kind':'inline_content','workspace_id':None,'relative_path':None,'display_name':value,'inline_content':inline,'version_hash':digest(inline)}
        if not loc: return
        identity=[profile,loc.get('workspace_id'),loc.get('relative_path') or loc.get('url') or [session['id'],loc.get('version_hash')]]
        rid=digest(identity)
        mid=str(message.get('message_uid') or message.get('id'))
        occurrence_id=digest([profile,message.get('message_uid') or [session['id'],mid],rid,action])
        if occurrence_id in seen: return
        seen.add(occurrence_id); count+=1
        time=message.get('timestamp')
        if not isinstance(time,(float,int)) or not math.isfinite(time): time=None
        occurrence={'id':occurrence_id,'stored_session_id':session['id'],'message_id':mid,'observed_at':time,'action':action,'outcome':action,'provenance':provenance,'source_chat_title':(session.get('title') or '')[:512]}
        row={'schema_version':SCHEMA_VERSION,'id':rid,'profile':profile,**loc,'stored_session_id':session['id'],'message_id':mid,'observed_at':time,'action':action,'outcome':action,'provenance':provenance,'source_chat_title':(session.get('title') or '')[:512],'display_type':mimetypes.guess_type(loc['display_name'])[0] or 'application/octet-stream','occurrences':[]}
        if subagent: row['subagent']=True
        if loc['kind']=='file':
            if rid not in availability:
                try:
                    fd=open_beneath(roots[loc['workspace_id']],loc['relative_path'],reveal=True)
                except (OSError,HTTPException): availability[rid]='unavailable'
                else:
                    os.close(fd); availability[rid]='available'
            row['availability']=availability[rid]
        existing=rows.get(rid)
        rank=(action in {'created','edited','delivered'},time is not None,time or 0,message_order.get(id(message),0))
        if existing:
            history=existing['occurrences']
            if rank>=ranks[rid]:
                row['occurrences']=history; rows[rid]=row; ranks[rid]=rank
            else: row=existing
        else: rows[rid]=row; ranks[rid]=rank
        row['occurrences'].append(occurrence)
        if loc['kind']=='file':
            low=loc['relative_path'].lower().split('/')
            row['private']=any(low[i:i+2] in (['outputs','private'],['evidence','private']) for i in range(len(low)))
    for m in messages:
        if m.get('active',1)==0 or m.get('display_kind') in {'reasoning','scratchpad'}: continue
        if m.get('role')=='assistant':
            content=m.get('content') or ''
            for _,text in strings(content,status):
                text=visible(text)
                for value,action in references(text, assistant=True): add(value,action,m,'assistant_reference')
                for match in re.finditer(r'```(html|svg|[a-zA-Z0-9_+-]+)\s*\n(.*?)\n```',text,re.S):
                    lang,body=match.groups()
                    threshold={'html':160,'svg':2000}.get(lang,3000)
                    if lang in {'markdown','md','text','log','diff','mermaid'}: continue
                    if len(body)>=threshold or (lang not in {'html','svg'} and body.count('\n')>=47):
                        add('From chat.'+lang,'delivered',m,'assistant_fence',inline=body)
            calls=decode(m.get('tool_calls') or [])
            if not isinstance(calls,list): continue
            if len(calls)>MAX_NODES: status['partial']=True
            for call in calls[:MAX_NODES]:
                if not isinstance(call,dict): continue
                function=call.get('function',call)
                if not isinstance(function,dict) or not isinstance(function.get('name',''),str):
                    status['partial']=True; continue
                name=function.get('name','').split('__')[-1].rsplit('.',1)[-1]
                action=MUTATIONS.get(name)
                args=decode(function.get('arguments',{}))
                if not action or not isinstance(args,dict): continue
                call_id=call.get('id')
                result=results.get(call_id) if isinstance(call_id,str) else None
                body=unwrap(result.get('content')) if result else None
                if result is None: outcome='pending'
                elif isinstance(body,dict) and (body.get('error') or body.get('success') is False or body.get('verified') is False): outcome='write_failed'
                elif action=='read': outcome='read'
                elif isinstance(body,dict) and (body.get('success') is True or body.get('verified') is True or 'bytes_written' in body): outcome=action
                else: outcome='pending'
                value=args.get('path') or args.get('file_path') or args.get('source')
                if value: add(value,outcome,result or m,'tool:'+name)
                destination=args.get('destination') or args.get('new_path')
                if destination: add(destination,outcome,result or m,'tool:'+name)
                if name.startswith(('delete','move')) and outcome==action and value:
                    loc=locator(value,session,workspaces,aliases)
                    if loc:
                        for row in rows.values():
                            if row.get('relative_path')==loc.get('relative_path') and row.get('workspace_id')==loc.get('workspace_id'): row['availability']='deleted'
        elif m.get('role')=='tool':
            name=(m.get('tool_name') or '').split('__')[-1].rsplit('.',1)[-1]
            body=unwrap(m.get('content'))
            if isinstance(body,dict) and (body.get('error') or body.get('success') is False or body.get('verified') is False): continue
            for key,text in strings(body,status):
                for value,action in references(text, assistant=True):
                    action='referenced' if not name or name in CONTENT_READERS else action
                    add(value,action,m,'tool_media')
                if name in PRODUCERS and key in OUTPUT_KEYS: add(text,'created',m,'producer:'+name)
    return list(rows.values())
