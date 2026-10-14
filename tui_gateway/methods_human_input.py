"""Owner-admitted actions routed to the existing process/compute request owner.

No runtime creation, no second queue, no value persistence, no mutation retries.
Saved connection/installation IDs are selectors, never authentication credentials.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import time

from . import server_requests
from .contracts.human_input import HumanInputScope, HumanInputRespondParams, HumanInputActionStatusParams

_server = None
_MODELS = {'pending': HumanInputScope, 'respond': HumanInputRespondParams, 'action-status': HumanInputActionStatusParams}
logger = logging.getLogger(__name__)
_SAFE_FAILURE_REASONS = frozenset({
    'Connection request has no chat owner', 'Runtime profile mismatch', 'Not an ordinary Chat',
    'Canonical Bot scope required', 'Runtime generation changed', 'Approval queue scope is ambiguous',
    'Owner required', 'Invalid human input request', 'Human input supervisor unavailable',
})


def _log_failure(exc, *, boundary, code=None):
    # Exception text can contain secure values or paths. Only these fixed literals are diagnostic.
    reason = exc.args[0] if len(exc.args) == 1 and type(exc.args[0]) is str else None
    reason = reason if reason in _SAFE_FAILURE_REASONS else 'redacted'
    logger.info('Human input rejected boundary=%s code=%s exception_class=%s reason=%s',
                boundary, code, type(exc).__name__, reason)


def _validate(operation, body):
    from pydantic import ValidationError
    try:
        raw = json.dumps(body, allow_nan=False)
        if len(raw.encode()) > 131072 or operation not in _MODELS:
            raise ValueError()
        return _MODELS[operation].model_validate_json(raw, strict=True).model_dump(mode='json', exclude_none=True)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ValueError('Invalid human input request') from None


def _scope_key(body, principal, home):
    return (principal, *(body[key] for key in ('connection_id', 'installation_id', 'profile', 'destination_kind',
                                             'stored_session_id', 'canonical_root_id', 'session_id')), home)


def _scope(server, body, principal):
    from hermes_cli.profiles import validate_profile_name
    validate_profile_name(body['profile'])
    home = str(Path(server._profile_home(body['profile']) or server._hermes_home).resolve())
    kind, sid = body['destination_kind'], body['session_id']
    session = server._sessions.get(sid) if sid else None
    if kind == 'connection':
        if sid or body['stored_session_id'] or body['canonical_root_id']:
            raise PermissionError('Connection request has no chat owner')
    else:
        if not session or str(Path(session.get('profile_home') or server._hermes_home).resolve()) != home:
            raise PermissionError('Runtime profile mismatch')
        if kind == 'chat':
            from .mobile_session_scope import resolve_destination, listed_chat
            destination, tip, _ = resolve_destination(body['profile'], home, body['stored_session_id'])
            if body['canonical_root_id'] or not listed_chat(home, destination.session_id):
                raise PermissionError('Not an ordinary Chat')
        else:
            from hermes_state import SessionDB
            from .methods_mobile import _mobile_canonical_identity
            if body['stored_session_id'] or not body['canonical_root_id']:
                raise PermissionError('Canonical Bot scope required')
            with SessionDB(db_path=Path(home) / 'state.db', read_only=True) as db:
                _, tip_row, _ = _mobile_canonical_identity(db, body['canonical_root_id'])
            tip = tip_row['id']
        if session.get('session_key') != tip:
            raise PermissionError('Runtime generation changed')
        # The old approval queue uses durable IDs; never let a colliding profile resolve it.
        if any(other is not session and other.get('session_key') == tip
               and str(Path(other.get('profile_home') or server._hermes_home).resolve()) != home
               for other in list(server._sessions.values())) and body.get('method') == 'approval':
            raise PermissionError('Approval queue scope is ambiguous')
    return _scope_key(body, principal, home), session


def _pending(sid, home):
    return {'protocol_version': 1, 'request_epoch': server_requests.inbox_epoch(), 'server_time': time.time(),
            'complete': True, 'unavailable': [], 'requests': server_requests.scoped_pending(sid, profile_home=home)}


def _local(operation, body, scope):
    if operation == 'pending':
        result = _pending(body['session_id'], scope[-1])
        if body['destination_kind'] == 'connection':
            result['requests'] = [req for req in result['requests'] if req['method'] == 'display.install.sudo']
        return result
    if body['destination_kind'] == 'connection' and body['method'] != 'display.install.sudo':
        raise PermissionError('Invalid connection request')
    if body['destination_kind'] == 'bot' and body['method'] == 'approval' and operation == 'respond':
        if body['result'].get('choice') not in ('once', 'deny'):
            raise PermissionError('Bot approval is once-only')
    if operation == 'action-status':
        return server_requests.action_status(body, scope=scope)
    return server_requests.scoped_respond(body, scope=scope)


def child_call(server, sid, payload):
    """Private supervisor pipe only: recheck exact child runtime/home before registry access."""
    operation = payload['operation']
    body = _validate(operation, payload['body'])
    scope = tuple(payload['scope'])
    session = server._sessions.get(sid)
    if (not session or body['session_id'] != sid or scope[-2] != sid
            or str(Path(session.get('profile_home') or server._hermes_home).resolve()) != scope[-1]):
        raise PermissionError('Child request ownership mismatch')
    return _local(operation, body, scope)


def _compute(server, operation, body, scope):
    try:
        supervisor = getattr(server, '_compute_host_supervisor', None)
        if supervisor is None:
            raise RuntimeError('Child unavailable')
        ack = supervisor.respond(body['session_id'],
            {'human_input': {'operation': operation, 'body': body, 'scope': list(scope)}}, start_if_missing=False)
        result = ack.get('response', {}).get('result')
        if ack.get('type') != 'respond.ack' or not isinstance(result, dict):
            raise RuntimeError('Child unavailable')
        if operation != 'pending':
            server_requests.remember_remote_receipt(body, scope=scope, receipt=result)
        return result
    except Exception:  # health: allow BLE001 -- child IPC errors may include secure input; never echo exception text
        # A lost ACK is NOT a refusal or permission to repeat an action.
        if operation == 'pending':
            return {'protocol_version': 1, 'request_epoch': server_requests.inbox_epoch(), 'server_time': time.time(),
                    'complete': False, 'unavailable': ['compute_child'], 'requests': []}
        return server_requests._receipt(body, 'outcome_unknown' if operation == 'respond' else 'unavailable')


def call(operation, body, *, principal):
    server = _server
    if server is None or os.environ.get('HERMES_COMPUTE_HOST_CHILD') == '1' or os.environ.get('HERMES_PLUGIN_HOST_PROCESS') == '1':
        raise RuntimeError('Human input supervisor unavailable')
    if not isinstance(principal, str) or not principal:
        raise PermissionError('Owner required')
    body = _validate(operation, body)
    if operation == 'action-status':
        # Receipts outlive their runtime, but contain no authority or answer value.
        home = str(Path(server._profile_home(body['profile']) or server._hermes_home).resolve())
        receipt = server_requests.action_status(body, scope=_scope_key(body, principal, home))
        if receipt['status'] != 'unknown' or body['session_id'] not in server._sessions:
            return receipt
    try:
        scope, session = _scope(server, body, principal)
    except (PermissionError, ValueError, TypeError, OSError) as exc:
        # REST dispatches call() directly, bypassing the native RPC error boundary.
        _log_failure(exc, boundary='scope')
        raise
    if session is not None and server._session_uses_compute_host(session):
        return _compute(server, operation, body, scope)
    return _local(operation, body, scope)


def _rpc(server, operation, rid, body):
    from hermes_cli.config import load_config
    from .methods_browser_control import _is_authenticated_identity
    identity = getattr(server.current_transport(), 'auth_identity', None)
    try:
        if not _is_authenticated_identity(identity):
            raise PermissionError('Owner required')
        parsed = _validate(operation, body)
        home = server._profile_home(parsed['profile']) or server._hermes_home
        with server._session_profile_runtime_scope({'profile_home': str(home)}, hydrate_secrets=False):
            config = load_config().get('cloudseed_mobile', {})
        if (identity['provider'], identity['user_id']) != (config.get('owner_provider'), config.get('owner_user_id')):
            raise PermissionError('Owner required')
        principal = hashlib.sha256(json.dumps(['cloudseed-mobile-owner-v1', identity['provider'], identity['user_id']], separators=(',', ':')).encode()).hexdigest()
        return server._ok(rid, call(operation, parsed, principal=principal))
    except PermissionError as exc:
        _log_failure(exc, boundary='rpc', code=4403)
        return server._err(rid, 4403, 'Human input scope denied')
    except (ValueError, TypeError, FileNotFoundError) as exc:
        _log_failure(exc, boundary='rpc', code=4400)
        return server._err(rid, 4400, 'Invalid human input request')
    except (RuntimeError, OSError) as exc:
        _log_failure(exc, boundary='rpc', code=4401)
        return server._err(rid, 4401, 'Human input unavailable')


def register(server_module, register_fn):
    global _server
    _server = server_module
    for operation in _MODELS:
        register_fn('human_input.' + operation.replace('-', '_'),
                    lambda rid, params, op=operation: _rpc(server_module, op, rid, params))
