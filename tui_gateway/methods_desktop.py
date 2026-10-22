"""Owner-admitted shared Desktop projections. No Cua control adapter is enabled."""
from __future__ import annotations

import sqlite3


_OPERATIONS = ('snapshot', 'observe', 'action', 'action_status')


class DesktopDenied(PermissionError):
    pass


def _desktop_scope(server, params):
    from hermes_cli.config import load_config
    from hermes_cli.profiles import _get_default_hermes_home, validate_profile_name
    from .methods_browser_control import _is_authenticated_identity

    transport = server.current_transport()
    identity = getattr(transport, 'auth_identity', None)
    if not _is_authenticated_identity(identity):
        raise DesktopDenied('Desktop owner required')
    root = _get_default_hermes_home()
    # A shared Space has one owner policy, not whichever profile is being viewed.
    with server._session_profile_runtime_scope({'profile_home': str(root)}, hydrate_secrets=False):
        policy = load_config().get('cloudseed_mobile', {})
    if (identity['provider'], identity['user_id']) != (policy.get('owner_provider'), policy.get('owner_user_id')):
        raise DesktopDenied('Desktop owner required')
    validate_profile_name(params['profile'])
    home = server._profile_home(params['profile']) or server._hermes_home
    from tools.desktop_spaces import owner_scope
    scope = owner_scope(identity['provider'], identity['user_id'], home, params['space'])
    return root, home, scope


def _desktop_rpc(server, operation, rid, params):
    from tools.desktop_spaces import SpaceStore
    from .contracts.desktop import (
        DesktopSnapshotParams, DesktopObserveParams, DesktopActionParams, DesktopActionStatusParams)
    models = {'snapshot': DesktopSnapshotParams, 'observe': DesktopObserveParams,
              'action': DesktopActionParams, 'action_status': DesktopActionStatusParams}
    try:
        params = models[operation].model_validate(params, strict=True).model_dump(mode='json')
        root, home, scope = _desktop_scope(server, params)
        with server._session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            store = SpaceStore(root / 'desktop-spaces.sqlite')
            resource = params['space']
            if operation == 'snapshot':
                result = store.snapshot(resource, scope, limit=params['limit'])
            elif operation == 'action_status':
                result = store.action_status(resource, scope, params['action_id'])
            elif operation == 'action' and params['action'] == 'not_now':
                result = store.not_now(resource, scope, params)
            else:
                # Local lease state cannot revoke old Cua activation tickets or stop agent input.
                return server._err(rid, 4002, 'Desktop operation is not available yet')
        return server._ok(rid, result)
    except server.ProfileUnavailableError:
        return server._err(rid, 4064, 'Desktop profile unavailable')
    except DesktopDenied:
        return server._err(rid, 4403, 'Desktop scope denied')
    except ValueError:
        return server._err(rid, 4000, 'Invalid desktop request')
    except TypeError:
        return server._err(rid, -32603, 'Desktop operation failed')
    except (OSError, sqlite3.Error):
        return server._err(rid, 5401, 'Desktop state unavailable')


def register(server):
    for operation in _OPERATIONS:
        server.register_method('desktop.' + operation,
                               lambda rid, params, op=operation: _desktop_rpc(server, op, rid, params))
    server._LONG_HANDLERS = server._LONG_HANDLERS | {'desktop.' + operation for operation in _OPERATIONS}
