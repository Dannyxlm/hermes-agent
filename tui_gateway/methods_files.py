"""Owner-only Files read RPCs; WebSocket dispatch already runs in a worker thread."""
import hashlib
import json
import re
import sqlite3

from fastapi import HTTPException
from pydantic import ValidationError

from plugins.cloudseed_mobile.dashboard.plugin_api import deliverable_store, owner_config
from plugins.cloudseed_mobile.links_index import query_index
from .contracts.files import FilesIndexParams, IndexParams
from .methods_browser_control import _is_authenticated_identity


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


def _rpc(server, links, rid, body):
    try:
        principal = _principal(server)
        params = (IndexParams if links else FilesIndexParams).model_validate(body).model_dump()
        profile = params['profile']
        if profile in {'all', 'current'} or not re.fullmatch(r'[A-Za-z0-9_-]+', profile):
            raise ValueError('Concrete profile required')
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
        return server._err(rid, code, 'Files index request denied' if code == 4403 else 'Invalid or stale Files index request' if code in {4400, 4091} else 'Files index unavailable')
    except (ValidationError, ValueError, TypeError, FileNotFoundError):
        return server._err(rid, 4400, 'Invalid Files index request')
    except (OSError, sqlite3.Error):
        return server._err(rid, 4401, 'Files index unavailable')


def register(server, register_fn):
    server._LONG_HANDLERS = server._LONG_HANDLERS | {'files.index', 'links.index'}
    for name, links in [('files.index', False), ('links.index', True)]:
        register_fn(name, lambda rid, params, links=links: _rpc(server, links, rid, params))
