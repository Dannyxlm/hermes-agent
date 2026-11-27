"""Owner-only Files read RPCs; WebSocket dispatch already runs in a worker thread."""
import base64
from collections import OrderedDict
import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading

from fastapi import HTTPException
from pydantic import ValidationError

from plugins.cloudseed_mobile.dashboard.plugin_api import deliverable_store, owner_config
from plugins.cloudseed_mobile.links_index import query_index
from .contracts.files import FilesIndexParams, IndexParams
from gateway.session_identity import is_authenticated_identity as _is_authenticated_identity

_PROFILE_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')
_ALL_PROFILES_CACHE_MAX = 64
_ALL_PROFILES_CACHE_LOCK = threading.Lock()
_ALL_PROFILES_CACHE: OrderedDict = OrderedDict()


def _all_profiles_fingerprint(server, profile_names):
    """Fast stat fingerprint across every profile's index/state/config files (~0.3ms total)."""
    rows = []
    for p_name in profile_names:
        try:
            home = Path(server._profile_home(p_name) or server._hermes_home)
        except server.ProfileUnavailableError:
            rows.append((p_name, None))
            continue
        p_stats = []
        for rel in (
            'state.db',
            'state.db-wal',
            'config.yaml',
            'plugin_data/cloudseed_mobile/files-index.sqlite3',
            'plugin_data/cloudseed_mobile/files-index.sqlite3-wal',
        ):
            try:
                st = (home / rel).stat()
                p_stats.append((st.st_ino, st.st_mtime_ns, st.st_size))
            except OSError:
                p_stats.append(None)
        rows.append((p_name, str(home), tuple(p_stats)))
    return tuple(rows)


def _principal(server):
    identity = getattr(server.current_transport(), 'auth_identity', None)
    if not _is_authenticated_identity(identity):
        raise PermissionError('Owner login required')
    config = owner_config()
    if not config.get('owner_provider') or not config.get('owner_user_id'):
        raise PermissionError('Owner not configured')
    if (identity['provider'], identity['user_id']) != (config['owner_provider'], config['owner_user_id']):
        raise PermissionError('Configured owner required')
    return hashlib.sha256(json.dumps(['cloudseed-mobile-owner-v1', identity['provider'], identity['user_id']], separators=(',', ':')).encode()).hexdigest()


def _refresh(server, home, index):
    with server._session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
        index.refresh()


def _all_profile_names(server, base_profile):
    names = []
    seen = set()
    for candidate in ('default', base_profile):
        if candidate and candidate not in seen:
            names.append(candidate)
            seen.add(candidate)
    profiles_dir = Path(server._hermes_home) / 'profiles'
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir(), key=lambda p: p.name):
            name = entry.name
            if name in seen or name.startswith('.') or not _PROFILE_NAME_RE.fullmatch(name):
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            if (entry / 'state.db').is_file() or (entry / 'config.yaml').is_file():
                names.append(name)
                seen.add(name)
    return names


def _encode_all_profiles_cursor(snapshot_id, query_digest, offset):
    payload = json.dumps({'v': 1, 's': snapshot_id, 'q': query_digest, 'o': offset}, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(payload).decode('ascii')


def _decode_all_profiles_cursor(raw):
    try:
        padded = raw + '=' * (-len(raw) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
        if (not isinstance(obj, dict) or obj.get('v') != 1
                or not isinstance(obj.get('s'), str) or not isinstance(obj.get('q'), str)
                or type(obj.get('o')) is not int or obj['o'] < 0):
            raise ValueError('bad cursor')
        return obj
    except Exception as exc:
        raise HTTPException(409, 'Stale or invalid cross-profile cursor') from exc


def _sum_total(current, incoming):
    if current is None or incoming is None:
        return None
    return current + incoming


def _rpc_all_profiles(server, links, principal, params):
    if (params.get('session_id') is not None or params.get('workspace_id') is not None
            or params.get('include_workspace') or params.get('directory_path') is not None):
        raise ValueError('Cross-profile query does not accept session or workspace filters')
    limit = params['limit']
    query_digest = hashlib.sha256(json.dumps(
        ['all-profiles-q-v1', 'links' if links else 'files',
         params.get('q', ''), params.get('author', 'all'), params.get('category', 'all')],
        separators=(',', ':')).encode()).hexdigest()
    cursor_obj = _decode_all_profiles_cursor(params['cursor']) if params.get('cursor') else None
    offset = cursor_obj['o'] if cursor_obj else 0
    if cursor_obj and cursor_obj['q'] != query_digest:
        raise HTTPException(409, 'Cursor query mismatch')

    profile_names = _all_profile_names(server, params['profile'])
    fp = _all_profiles_fingerprint(server, profile_names)
    ap_cache_key = (
        principal,
        bool(links),
        params['profile'],
        query_digest,
        offset,
        limit,
        fp,
    )
    with _ALL_PROFILES_CACHE_LOCK:
        cached_ap = _ALL_PROFILES_CACHE.get(ap_cache_key)
        if cached_ap is not None:
            _ALL_PROFILES_CACHE.move_to_end(ap_cache_key)
            cached_ap = copy.deepcopy(cached_ap)
    if cached_ap is not None:
        if params.get('snapshot_id') and params['snapshot_id'] != cached_ap['snapshot_id']:
            raise HTTPException(409, 'Snapshot stale')
        if cursor_obj and cursor_obj['s'] != cached_ap['snapshot_id']:
            raise HTTPException(409, 'Cursor snapshot stale')
        return cached_ap
    per_profile_limit = min(200, offset + limit + 1)
    revisions = []
    merged_items = []
    totals = {'media': 0, 'files': 0, 'links': 0, 'messages': 0}
    complete = True
    refresh_pending = False
    latest_updated_at = None
    any_more_upstream = False

    for p_name in profile_names:
        try:
            home = server._profile_home(p_name) or server._hermes_home
        except server.ProfileUnavailableError:
            continue
        if p_name != 'default' and not (Path(home) / 'state.db').is_file():
            continue
        sub_params = {
            **params,
            'profile': p_name,
            'cursor': None,
            'snapshot_id': None,
            'limit': per_profile_limit,
            'all_profiles': False,
        }
        try:
            with server._session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
                index = deliverable_store(p_name, files_index=True)
                res = query_index(index, principal, sub_params, links=links)
                if res['coverage']['refresh_pending']:
                    server._pool.submit(_refresh, server, home, index)
        except (PermissionError, server.ProfileUnavailableError, HTTPException, OSError, sqlite3.Error, ValueError):
            complete = False
            continue
        revisions.append([p_name, res['index_revision']])
        merged_items.extend(res['items'])
        for key in totals:
            totals[key] = _sum_total(totals[key], res['totals'].get(key))
        cov = res['coverage']
        complete = complete and bool(cov.get('complete'))
        refresh_pending = refresh_pending or bool(cov.get('refresh_pending'))
        upd = cov.get('updated_at')
        if isinstance(upd, (int, float)) and (latest_updated_at is None or upd > latest_updated_at):
            latest_updated_at = upd
        if res.get('next_cursor') is not None:
            any_more_upstream = True

    index_revision = hashlib.sha256(json.dumps(['all-profiles-rev-v1', revisions], separators=(',', ':')).encode()).hexdigest()
    snapshot_id = hashlib.sha256(json.dumps(
        ['all-profiles-snap-v1', principal, index_revision, query_digest], separators=(',', ':')).encode()).hexdigest()
    if params.get('snapshot_id') and params['snapshot_id'] != snapshot_id:
        raise HTTPException(409, 'Snapshot stale')
    if cursor_obj and cursor_obj['s'] != snapshot_id:
        raise HTTPException(409, 'Cursor snapshot stale')

    merged_items.sort(
        key=lambda item: (
            item.get('observed_at') is not None,
            item.get('observed_at') if isinstance(item.get('observed_at'), (int, float)) else 0.0,
            item.get('profile') or '',
            item.get('id') or '',
        ),
        reverse=True,
    )
    page_items = merged_items[offset:offset + limit]
    has_more = (len(merged_items) > offset + limit) or any_more_upstream
    next_cursor = _encode_all_profiles_cursor(snapshot_id, query_digest, offset + limit) if (has_more and page_items) else None
    result = {
        'schema_version': 1,
        'profile': params['profile'],
        'snapshot_id': snapshot_id,
        'index_revision': index_revision,
        'next_cursor': next_cursor,
        'totals': totals,
        'coverage': {
            'complete': complete,
            'refresh_pending': refresh_pending,
            'updated_at': latest_updated_at,
        },
        'items': page_items,
    }
    if not refresh_pending:
        with _ALL_PROFILES_CACHE_LOCK:
            _ALL_PROFILES_CACHE[ap_cache_key] = copy.deepcopy(result)
            _ALL_PROFILES_CACHE.move_to_end(ap_cache_key)
            while len(_ALL_PROFILES_CACHE) > _ALL_PROFILES_CACHE_MAX:
                _ALL_PROFILES_CACHE.popitem(last=False)
    return result


def _rpc(server, links, rid, body):
    try:
        principal = _principal(server)
        params = (IndexParams if links else FilesIndexParams).model_validate(body).model_dump()
        profile = params['profile']
        if profile in {'all', 'current'} or not _PROFILE_NAME_RE.fullmatch(profile):
            raise ValueError('Concrete profile required')
        if params.get('all_profiles'):
            return server._ok(rid, _rpc_all_profiles(server, links, principal, params))
        home = server._profile_home(profile) or server._hermes_home
        with server._session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            index = deliverable_store(profile, files_index=True)
            result = query_index(index, principal, params, links=links)
            if result['coverage']['refresh_pending']:
                server._pool.submit(_refresh, server, home, index)
        return server._ok(rid, result)
    except (PermissionError, server.ProfileUnavailableError):
        return server._err(rid, 4403, 'Files owner scope denied')
    except HTTPException as exc:
        code = {400: 4400, 403: 4403, 404: 4400, 409: 4091}.get(exc.status_code, 4401)
        return server._err(rid, code, 'Files index request denied' if code == 4403 else 'Invalid or stale Files index request' if code in {400, 4091} else 'Files index unavailable')
    except (ValidationError, ValueError, TypeError, FileNotFoundError):
        return server._err(rid, 4400, 'Invalid Files index request')
    except (OSError, sqlite3.Error):
        return server._err(rid, 4401, 'Files index unavailable')


def register(server, register_fn):
    server._LONG_HANDLERS = server._LONG_HANDLERS | {'files.index', 'links.index'}
    for name, links in [('files.index', False), ('links.index', True)]:
        register_fn(name, lambda rid, params, links=links: _rpc(server, links, rid, params))
