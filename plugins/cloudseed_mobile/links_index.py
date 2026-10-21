"""Admitted link evidence and mixed queries over the existing deliverables cache."""
import base64
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
from urllib.parse import urlsplit

from fastapi import HTTPException

from hermes_state_holders import read_only_db_uri
from plugins.cloudseed_mobile.deliverables import MAX_REFERENCES, MAX_STRING, decode, delegate_child, is_image, strings, visible
from plugins.cloudseed_mobile.workspace_files import digest, open_beneath, parts


def mention(kind, target):
    from agent.context_references import format_reference_value, parse_context_references
    if not isinstance(target, str) or any(ord(c) < 32 for c in target):
        return None
    text = '@' + kind + ':' + format_reference_value(target)
    parsed = parse_context_references(text)
    return text if len(parsed) == 1 and parsed[0].target == target else None


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 for c in value):
        return False
    try:
        url = urlsplit(value)
        host = url.hostname
        if url.scheme not in {'http', 'https'} or not host or url.username or url.password:
            return False
        if host.lower() == 'localhost' or host.lower().endswith(('.local', '.localhost')):
            return False
        _ = url.port
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return not any(c in host for c in '\\<>`\"\'')
    except ValueError:
        return False


def author_fields(profile, message):
    role = message.get('role')
    kind = 'me' if role == 'user' else 'agent' if role in {'assistant', 'tool'} else 'unknown'
    return {'author_kind': kind, 'author_profile': profile if kind == 'agent' else None}


def source_occurrence(profile, session, message, rid):
    mid = str(message.get('message_uid') or message.get('id'))
    observed = message.get('timestamp')
    if type(observed) not in (int, float) or not math.isfinite(observed):
        observed = None
    return {'id': digest([profile, session['id'], mid, rid]), 'stored_session_id': session['id'],
            'message_id': mid, 'observed_at': observed, 'source_chat_title': session.get('title'),
            'action': 'referenced', 'outcome': 'referenced', 'provenance': 'message_link',
            **author_fields(profile, message)}


def link_candidates(content, status):
    for _, text in strings(content, status):
        text = visible(text)
        explicit = re.compile(r'(?<!!)\[([^\]\n]*)\]\((https?://[^\s)]+)\)')
        for match in explicit.finditer(text):
            if public_url(match[2]):
                yield match[2], match[1][:512] or None
        for match in re.finditer(r'https?://[^\s<>\"\[\]`]+', explicit.sub(' ', text)):
            url = match[0].rstrip('.,)')
            if public_url(url):
                yield url, None


def project_links(profile, session, messages, workspaces, status):
    rows = {}
    cwd = Path(session.get('cwd') or '/')
    workspace_id = next((w['id'] for w in sorted(workspaces, key=lambda w: len(w['root_path']), reverse=True)
                         if cwd.is_relative_to(Path(w['root_path']))), None)
    for message in messages:
        if message.get('role') not in {'user', 'assistant'} or message.get('active', 1) == 0:
            continue
        if message.get('display_kind') in {'reasoning', 'thinking', 'scratchpad'}:
            continue
        for ordinal, (url, title) in enumerate(link_candidates(message.get('content'), status)):
            if ordinal >= MAX_REFERENCES:
                status['links_partial'] = True
                break
            rid = digest(['link-v1', profile, url])
            occurrence = source_occurrence(profile, session, message, rid)
            previous = rows.get(rid)
            history = {o['id']: o for o in previous['occurrences']} if previous else {}
            history[occurrence['id']] = occurrence
            row = {'id': rid, 'profile': profile, 'kind': 'link', 'url': url, 'title': title,
                   'display_name': title or url, 'workspace_id': workspace_id,
                   **{k: v for k, v in occurrence.items() if k != 'id'},
                   'tag': None, 'mention_text': mention('url', url), 'occurrences': list(history.values())[-100:]}
            if delegate_child(session):
                row['subagent'] = True
            row['occurrences_partial'] = len(history) > 100 or bool(previous and previous.get('occurrences_partial'))
            rows[rid] = row
    if status.get('truncated'):
        status['links_partial'] = True
    return list(rows.values())


def attachment_paths(message):
    """Only structured user attachment blocks carry file intent, not arbitrary prose."""
    if message.get('role') != 'user':
        return
    blocks = decode(message.get('content'))
    blocks = blocks[:512] if isinstance(blocks, list) else []
    metadata = decode(message.get('display_metadata'))
    if isinstance(metadata, dict) and isinstance(metadata.get('attachments'), list):
        blocks += [{**block, 'type': block.get('type', 'attachment')} for block in metadata['attachments'][:512]
                   if isinstance(block, dict)]
    for block in blocks:
        if not isinstance(block, dict) or block.get('type') not in {'file', 'input_file', 'image_url', 'input_image', 'attachment'}:
            continue
        value = block.get('file') or block.get('image_url') or block
        if isinstance(value, dict):
            value = value.get('path') or value.get('file_path') or value.get('url') or value.get('image_url')
        if isinstance(value, str):
            yield value


def project_attachments(messages, add, status):
    for message in messages:
        if message.get('active', 1) == 0 or message.get('display_kind') in {'reasoning', 'thinking', 'scratchpad'}:
            continue
        metadata = decode(message.get('display_metadata'))
        attachments = metadata.get('attachments') if isinstance(metadata, dict) else None
        if isinstance(attachments, list) and len(attachments) > 512:
            status['links_partial'] = True
        if isinstance(metadata, str) and len(metadata) > MAX_STRING:
            status['links_partial'] = True
        for value in attachment_paths(message):
            add(value, 'referenced', message, 'user_attachment')


def media(display_name, mime):
    return is_image(display_name, mime) or (mime or '').startswith(('audio/', 'video/'))


def _decode_cursor(cursor, scope):
    if cursor is None:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor))
        after = data['after']
        if data['scope'] != scope or len(after) != 3:
            raise ValueError()
        if type(after[0]) is not int or after[0] not in (0, 1) or type(after[1]) not in (int, float):
            raise ValueError()
        if not math.isfinite(after[1]) or not isinstance(after[2], str):
            raise ValueError()
        return after
    except (ValueError, TypeError, KeyError):
        raise HTTPException(409, 'Invalid or stale index cursor') from None


def _admission(db, index, params):
    source = index.home / 'state.db'
    if not source.is_file() or source.is_symlink():
        raise HTTPException(503, 'Session store unavailable')
    db.execute('ATTACH DATABASE ? AS source', (read_only_db_uri(source),))
    columns = {r[1] for r in db.execute('PRAGMA source.table_info(sessions)')}
    clauses = ['e.generation=?', 'e.private=0', "coalesce(json_extract(e.data,'$.subagent'),0)=0"]
    values = []
    for column in ('archived', 'hidden'):
        if column in columns:
            clauses.append('coalesce(s.' + column + ',0)=0')
    sid = params['session_id']
    if sid is not None:
        where = ' AND '.join(c.replace('s.', '') for c in clauses[3:])
        row = db.execute('SELECT id FROM source.sessions WHERE id=?' + (' AND ' + where if where else ''), (sid,)).fetchone()
        if row is None:
            raise HTTPException(400, 'Unknown explicit session')
        lineage = sorted(index._lineage(db, sid))
        clauses.append('e.sid IN (' + ','.join('?' for _ in lineage) + ')')
        values.extend(lineage)
    wid = params['workspace_id']
    if wid is not None:
        if wid not in {w['id'] for w in index.workspaces}:
            raise HTTPException(403, 'Workspace denied')
        clauses.append('e.workspace_id=?')
        values.append(wid)
    if params['q']:
        clauses.append('instr(e.search,?)>0')
        values.append(params['q'].casefold())
    if params['author'] != 'all':
        clauses.append("json_extract(o.value,'$.author_kind')=?")
        values.append('agent' if params['author'] == 'agents' else 'me')
    return clauses, values


def workspace_children(index, params):
    if not params.get('include_workspace'):
        if params.get('directory_path') is not None:
            raise HTTPException(400, 'Directory requires workspace inclusion')
        return [], None
    wid = params['workspace_id']
    workspace = next((w for w in index.workspaces if w['id'] == wid), None)
    if workspace is None:
        raise HTTPException(403, 'Workspace required')
    path = params.get('directory_path') or ''
    parts(path)
    fd = open_beneath(workspace['root_path'], path, directory=True)
    children = []
    try:
        with os.scandir(fd) as entries:
            for ordinal, entry in enumerate(entries):
                if ordinal >= 200:
                    break
                relative = str(PurePosixPath(path) / entry.name)
                try:
                    parts(relative)
                except HTTPException:
                    continue
                info = entry.stat(follow_symlinks=False)
                directory = stat.S_ISDIR(info.st_mode)
                if not (directory or stat.S_ISREG(info.st_mode)):
                    continue
                if params['q'].casefold() not in relative.casefold():
                    continue
                mime = mimetypes.guess_type(relative)[0]
                children.append({'id': digest([index.profile, wid, relative]), 'profile': index.profile,
                                 'kind': 'workspace_directory' if directory else 'file', 'display_name': entry.name,
                                 'relative_path': relative, 'workspace_id': wid, 'stored_session_id': None,
                                 'message_id': None, 'source_chat_title': None, 'observed_at': None,
                                 'byte_size': None if directory else info.st_size, 'display_type': mime,
                                 'author_kind': 'unknown', 'author_profile': None, 'is_directory': directory,
                                 'collection': None, 'mention_text': mention('folder' if directory else 'file', str(Path(workspace['root_path']) / relative))})
    finally:
        os.close(fd)
    children.sort(key=lambda r: r['id'])
    return children, digest(children)


def _query_sql(clauses):
    return '''WITH evidence AS (
        SELECT e.id,e.kind,e.data,o.value AS occurrence,
               json_extract(o.value,'$.observed_at') IS NULL AS sort_null,
               -coalesce(json_extract(o.value,'$.observed_at'),0) AS sort_time,
               CASE WHEN e.kind='link' THEN 'links'
                    WHEN is_media(json_extract(e.data,'$.display_name'),json_extract(e.data,'$.display_type')) THEN 'media'
                    ELSE 'files' END AS category
        FROM entries e JOIN source.sessions s ON s.id=e.sid JOIN json_each(e.data,'$.occurrences') o
        WHERE ''' + ' AND '.join(clauses) + '''), ranked AS (
        SELECT *,row_number() OVER (PARTITION BY id ORDER BY sort_null,sort_time,json_extract(occurrence,'$.id')) AS rank
        FROM evidence), matched AS (SELECT * FROM ranked WHERE rank=1) '''


def file_mention(index, row):
    if row.get('kind') != 'file':
        return mention('url', row.get('url'))
    workspace = next((w for w in index.workspaces if w['id'] == row.get('workspace_id')), None)
    if workspace is None or not row.get('relative_path'):
        return None
    try:
        fd = open_beneath(workspace['root_path'], row['relative_path'])
    except (OSError, HTTPException):
        return None
    os.close(fd)
    return mention('file', str(Path(workspace['root_path']) / row['relative_path']))


def _item(index, raw, occurrence, link):
    row = json.loads(raw)
    source = json.loads(occurrence)
    for key in ('stored_session_id', 'message_id', 'source_chat_title', 'observed_at', 'author_kind', 'author_profile'):
        row[key] = source.get(key)
    row.setdefault('author_kind', 'unknown')
    row['author_kind'] = row['author_kind'] or 'unknown'
    if link:
        keys = ('id', 'profile', 'url', 'title', 'stored_session_id', 'message_id', 'source_chat_title',
                'observed_at', 'workspace_id', 'author_kind', 'author_profile', 'tag', 'mention_text')
        return {k: row.get(k) for k in keys}
    row.update(is_directory=False, collection=None)
    row.pop('artifact_first_seen', None)
    row['mention_text'] = file_mention(index, row)
    row['occurrences'] = [source]
    return row


def workspace_counts(index):
    """Optional folder counts never turn a working workspace browser into an error."""
    counts = {w['id']: None for w in index.workspaces}
    with index.connection(uri=True) as db:
        db.execute('BEGIN')
        meta = index.snapshot(db)
        if meta.get('partial', True) or meta.get('links_partial') or meta['refresh_pending']:
            return counts
        params = dict(session_id=None, workspace_id=None, q='', author='all')
        clauses, values = _admission(db, index, params)
        deadline = time.monotonic() + 0.75
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        sql = '''SELECT e.workspace_id,count(DISTINCT e.id),
                 max(coalesce(json_extract(e.data,'$.occurrences_partial'),0))
                 FROM entries e JOIN source.sessions s ON s.id=e.sid
                 WHERE ''' + ' AND '.join(clauses) + ' GROUP BY e.workspace_id'
        rows = list(db.execute(sql, [meta.get('generation'), *values]))
        counts = {w['id']: 0 for w in index.workspaces}
        for wid, count, partial in rows:
            if wid in counts:
                counts[wid] = None if partial else count
    return counts


def _snapshot(index, principal, meta):
    from plugins.cloudseed_mobile.deliverables_index import source_signature
    revision = meta.get('index_revision') or digest(['cold', index.profile])
    token = digest([principal, str(index.home), index.profile, revision, meta.get('generation'),
                    meta.get('refresh_progress'), meta['refresh_pending'], meta.get('partial'), meta.get('links_partial'),
                    digest(index.workspaces), source_signature(index.home)])
    return revision, token


def _totals(db, sql, args, meta):
    counts = dict(db.execute(sql + 'SELECT category,count(*) FROM matched GROUP BY category', args))
    messages = db.execute(sql + "SELECT count(*) FROM (SELECT DISTINCT json_extract(occurrence,'$.stored_session_id'),json_extract(occurrence,'$.message_id') FROM evidence)", args).fetchone()[0]
    truncated = db.execute(sql + "SELECT 1 FROM evidence WHERE json_extract(data,'$.occurrences_partial')=1 LIMIT 1", args).fetchone()
    complete = not meta.get('partial', True) and not meta.get('links_partial') and not meta['refresh_pending'] and not truncated
    totals = {key: counts.get(key, 0) if complete else None for key in ('media', 'files', 'links')}
    totals['messages'] = messages if complete else None
    return totals, complete


def _page_candidates(db, index, sql, values, params, links, after):
    item_filter = "category='links'" if links else "category!='links'"
    args = list(values)
    if not links and params.get('category', 'all') != 'all':
        item_filter += ' AND category=?'
        args.append(params['category'])
    if after:
        item_filter += ' AND (sort_null,sort_time,id)>(?,?,?)'
        args.extend(after)
    fetched = list(db.execute(sql + 'SELECT id,sort_null,sort_time,data,occurrence FROM matched WHERE ' + item_filter + ' ORDER BY sort_null,sort_time,id LIMIT ?', [*args, params['limit'] + 1]))
    return [(null, timestamp, rid, _item(index, data, occurrence, links)) for rid, null, timestamp, data, occurrence in fetched]


def _append_children(db, sql, args, candidates, children, params, after):
    if not children or params['author'] != 'all':
        return
    keys = [row['id'] for row in children]
    present = {r[0] for r in db.execute(sql + 'SELECT id FROM matched WHERE id IN (' + ','.join('?' for _ in keys) + ')', [*args, *keys])}
    for row in children:
        category = params.get('category', 'all')
        if category != 'all' and (row['is_directory'] or media(row['display_name'], row.get('display_type')) != (category == 'media')):
            continue
        if row['id'] not in present and (after is None or (1, 0, row['id']) > tuple(after)):
            candidates.append((1, 0, row['id'], row))


def _file_metadata(db, index, sql, args, items):
    if not items:
        return
    ids = [r['id'] for r in items if r['message_id'] is not None]
    if ids:
        history_sql = sql + "SELECT id,occurrence FROM (SELECT id,occurrence,row_number() OVER (PARTITION BY id ORDER BY sort_null,sort_time) AS ordinal FROM evidence WHERE id IN (" + ','.join('?' for _ in ids) + ')) WHERE ordinal<=101'
        histories = {}
        for rid, occurrence in db.execute(history_sql, [*args, *ids]):
            histories.setdefault(rid, []).append(json.loads(occurrence))
        for row in items:
            if row['id'] in histories:
                row['occurrences'] = histories[row['id']][:100]
                row['occurrences_partial'] = bool(row.get('occurrences_partial')) or len(histories[row['id']]) > 100
    artifacts = index._artifacts(db, args[0], [r.get('artifact_key') for r in items])
    for row in items:
        versions = artifacts.get(row.get('artifact_key'))
        if versions:
            row['version_count'] = len(versions)
            row['version_index'] = next((v['index'] for v in versions if v['version_hash'] == row.get('version_hash')), None)


def query_index(index, principal, params, *, links=False):
    children, directory_revision = workspace_children(index, params)
    with index.connection(uri=True) as db:
        db.execute('BEGIN')
        meta = index.snapshot(db)
        revision, snapshot = _snapshot(index, principal, meta)
        if params['snapshot_id'] is not None and params['snapshot_id'] != snapshot:
            raise HTTPException(409, 'Stale index snapshot')
        clauses, values = _admission(db, index, params)
        scope = digest([principal, snapshot, links, params['session_id'], params['workspace_id'], params['q'],
                        params['author'], params.get('category'), params.get('include_workspace'),
                        params.get('directory_path'), directory_revision])
        after = _decode_cursor(params['cursor'], scope)
        db.create_function('is_media', 2, media, deterministic=True)
        deadline = time.monotonic() + 0.75
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        sql = _query_sql(clauses)
        args = [meta.get('generation'), *values]
        totals, complete = _totals(db, sql, args, meta)
        candidates = _page_candidates(db, index, sql, args, params, links, after)
        if not links:
            _append_children(db, sql, args, candidates, children, params, after)
        candidates.sort(key=lambda r: r[:3])
        page = candidates[:params['limit']]
        items = [r[3] for r in page]
        if not links:
            _file_metadata(db, index, sql, args, items)
        cursor = base64.urlsafe_b64encode(json.dumps({'scope': scope, 'after': page[-1][:3]}).encode()).decode() if len(candidates) > len(page) and page else None
        if _snapshot(index, principal, meta)[1] != snapshot:
            raise HTTPException(409, 'Source changed during index query')
        return {'schema_version': 1, 'profile': index.profile, 'snapshot_id': snapshot, 'index_revision': revision,
                'items': items, 'next_cursor': cursor, 'totals': totals,
                'coverage': {'complete': complete, 'refresh_pending': meta['refresh_pending'], 'updated_at': meta.get('updated_at')}}
