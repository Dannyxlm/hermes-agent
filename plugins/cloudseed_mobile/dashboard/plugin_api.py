"""Native dashboard API. Global auth verifies sessions; this plugin requires its owner.

All domain IO runs in synchronous routes (FastAPI's context-copying threadpool).
Only the bounded JSON dependency reads asynchronously. No WebUI imports/runtime.
"""
import base64
import errno
import hashlib
import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from hermes_cli.config import load_config
from plugins.cloudseed_mobile.scope import profile_id, state_dir
from plugins.cloudseed_mobile.photo_catalog import PhotoCatalog, PhotoError, PhotoStorageFull
from plugins.cloudseed_mobile.iphone_reminders import ReminderOutbox, ReminderError
from plugins.cloudseed_mobile import provider_accounts as accounts
from plugins.cloudseed_mobile.memory_files import MAX_MEMORY, read_memory, write_memory

MAX_BODY = 1024 * 1024


def owner_config():
    return load_config().get('cloudseed_mobile', {})


async def owner(request: Request):
    session = getattr(request.state, 'session', None)
    if session is None:
        # Explicitly reject local no-auth dashboard and service token exemptions.
        raise HTTPException(401, 'Owner login required')
    cfg = owner_config()
    if (not cfg.get('owner_user_id') or not cfg.get('owner_provider') or
            session.user_id != cfg['owner_user_id'] or session.provider != cfg['owner_provider']):
        raise HTTPException(403, 'Configured owner required')
    # Cookie clients must retain a same-origin write boundary, independently of
    # bearer clients. Never trust forwarded host headers here.
    origin = request.headers.get('origin')
    if request.method != 'GET' and not request.headers.get('authorization', '').lower().startswith('bearer '):
        if origin != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Same-origin request required')
    return hashlib.sha256(json.dumps(['cloudseed-mobile-owner-v1', session.provider, session.user_id], separators=(',', ':')).encode()).hexdigest()


async def payload(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            raise HTTPException(413, 'Mobile request too large')
    try:
        body = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise HTTPException(400, 'JSON object required') from None
    if not isinstance(body, dict):
        raise HTTPException(400, 'JSON object required')
    return body


router = APIRouter(dependencies=[Depends(owner)])


def photo_store():
    return PhotoCatalog(state_dir() / 'photo-catalog/catalog.sqlite3')


def reminder_store():
    return ReminderOutbox(state_dir() / 'iphone-reminders/outbox.sqlite3')


def device_args(principal, body):
    profile = profile_id()
    if body.get('profile') != profile:
        raise HTTPException(403, 'Profile not found')
    return principal, profile, body.get('device_id'), body.get('secret')


def guarded(fn):
    try:
        return fn()
    except PhotoStorageFull:
        return JSONResponse({'error': 'Photo sync paused: server storage is low', 'code': 'storage_low'}, status_code=507)
    except accounts.AccountControlError as exc:
        return JSONResponse({'error': str(exc)}, status_code=exc.status)
    except OverflowError:
        return JSONResponse({'error': 'Memory file too large'}, status_code=413)
    except (PhotoError, ReminderError, ValueError, TypeError, RecursionError):
        return JSONResponse({'error': 'Invalid or unavailable mobile request'}, status_code=400)
    except OSError as exc:
        status = 400 if exc.errno in (errno.ELOOP, errno.ENOTDIR) else 503
        return JSONResponse({'error': 'Mobile storage unavailable'}, status_code=status)
    except sqlite3.Error:
        return JSONResponse({'error': 'Mobile storage unavailable'}, status_code=503)


PHOTO_OPS = ('capabilities', 'register', 'begin', 'manifest', 'put', 'finish', 'disconnect', 'status', 'search', 'preview')
REMINDER_OPS = ('register', 'poll', 'ack', 'disconnect', 'status')


def photo_call(op, principal, body):
    args = device_args(principal, body)
    if op == 'capabilities':
        return {'ok': True, 'protocol_version': 1}
    store = photo_store()
    if op == 'register':
        if 'rebind' in body and type(body['rebind']) is not bool:
            raise PhotoError('Invalid rebind')
        return store.rebind(*args) if body.get('rebind') else store.register(*args)
    if op == 'begin':
        return store.begin(*args, body.get('count'))
    if op == 'manifest':
        return store.manifest(*args, body.get('epoch'), body.get('items'))
    if op == 'put':
        return store.put(*args, body.get('epoch'), body.get('item'))
    if op == 'finish':
        return store.finish(*args, body.get('epoch'))
    if op == 'disconnect':
        return store.disconnect(*args)
    if op == 'status':
        return store.status(args[1], args[2], principal)
    if op == 'search':
        return store.search(args[1], args[2], body.get('query', {}), principal)
    return {'ok': True, 'protocol_version': 1, 'preview': base64.b64encode(store.preview(args[1], args[2], body.get('asset_id'), principal)).decode()}


def reminder_call(op, principal, body):
    args = device_args(principal, body)
    store = reminder_store()
    if op == 'register':
        if 'rebind' in body and type(body['rebind']) is not bool:
            raise ReminderError('Invalid rebind')
        return store.rebind(*args) if body.get('rebind') else store.register(*args)
    if op == 'poll':
        return store.poll(*args)
    if op == 'ack':
        return store.acknowledge(*args, body.get('request_id'), body.get('state'), body.get('result'))
    if op == 'disconnect':
        return store.disconnect(*args)
    # Keep old capability shape, but reject unregistered/foreign device IDs.
    with store.connection() as db:
        store._device(db, args[2], principal, args[1], args[3])
    return {'ok': True, 'protocol_version': 1}


# Explicit route enumeration: no arbitrary path/operation dispatch.
def photo_endpoint(op):
    def endpoint(body: dict = Depends(payload), principal: str = Depends(owner)):
        return guarded(lambda: photo_call(op, principal, body))
    return endpoint


def reminder_endpoint(op):
    def endpoint(body: dict = Depends(payload), principal: str = Depends(owner)):
        return guarded(lambda: reminder_call(op, principal, body))
    return endpoint


for operation in PHOTO_OPS:
    router.add_api_route('/photo-catalog/'+operation, photo_endpoint(operation), methods=['POST'], name='photo_'+operation)
for operation in REMINDER_OPS:
    router.add_api_route('/iphone-reminders/'+operation, reminder_endpoint(operation), methods=['POST'], name='reminder_'+operation)


@router.get('/provider/accounts')
def list_accounts():
    return guarded(accounts.get_accounts)


@router.get('/provider/accounts/usage')
def account_usage(provider: str, refresh: bool = False):
    return guarded(lambda: accounts.get_account_usage(provider, refresh=refresh))


@router.post('/provider/accounts/primary')
def account_primary(body: dict = Depends(payload)):
    return guarded(lambda: accounts.set_primary(body))


@router.get('/memory')
def memory_read():
    return guarded(read_memory)


@router.post('/memory/write')
def memory_write(body: dict = Depends(payload)):
    return guarded(lambda: write_memory(body.get('section'), body.get('content')))
