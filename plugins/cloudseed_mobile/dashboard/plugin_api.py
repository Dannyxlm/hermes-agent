"""Native dashboard API. Global auth verifies sessions; this plugin requires its owner.

All domain IO runs in synchronous routes (FastAPI's context-copying threadpool).
Only the bounded JSON dependency reads asynchronously. No WebUI imports/runtime.
"""
import base64
import errno
import hashlib
import json
import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from hermes_cli.config import load_config
from plugins.cloudseed_mobile.scope import profile_id, state_dir
from plugins.cloudseed_mobile.photo_catalog import PhotoCatalog, PhotoError, PhotoStorageFull
from plugins.cloudseed_mobile.iphone_reminders import ReminderOutbox, ReminderError
from plugins.cloudseed_mobile import provider_accounts as accounts
from plugins.cloudseed_mobile.memory_files import MAX_MEMORY, read_memory, write_memory
from plugins.cloudseed_mobile.session_activity import session_activity, session_activity_batch
from plugins.cloudseed_mobile import workspace_files as files
from plugins.cloudseed_mobile import flight_recorder as recorder

MAX_BODY = 1024 * 1024


def owner_config():
    return load_config().get('cloudseed_mobile', {})


def owner(request: Request):
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
    if request.method not in ('GET', 'HEAD') and not request.headers.get('authorization', '').lower().startswith('bearer '):
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
    # This is the phone's pre-pair capability probe. Supplied IDs are never
    # silently accepted; only an absent device ID is capability-only.
    if op == 'status' and 'device_id' not in body and 'secret' not in body:
        return {'ok': True, 'protocol_version': 1}
    store = reminder_store()
    # `_device` also serves owner-local CLI callers that do not have a secret;
    # HTTP device operations must never take that optional-secret branch.
    store._digest(args[3])
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


def deliverable_store(profile):
    from hermes_constants import get_hermes_home
    from plugins.cloudseed_mobile.deliverables_index import DeliverablesIndex
    files.check_profile(profile)
    return DeliverablesIndex(get_hermes_home(), profile, files.workspaces(profile, include_generation=True)['items'])


@router.get('/deliverables')
def deliverable_list(background_tasks: BackgroundTasks, profile: str, workspace_id: str | None = None, session_id: str | None = None, kind: str | None = None, q: str = '', cursor: str | None = None, limit: int = 50, reveal: bool = False):
    def run():
        index = deliverable_store(profile)
        result = index.query(workspace_id, session_id, kind, q, cursor, limit, reveal)
        background_tasks.add_task(index.refresh)
        return result
    return guarded(run)


@router.get('/deliverables/content')
def deliverable_content(profile: str, id: str, reveal: bool = False):
    return guarded(lambda: deliverable_store(profile).content(id, reveal))


@router.get('/deliverables/versions')
def deliverable_versions(profile: str, artifact_key: str):
    return guarded(lambda: deliverable_store(profile).versions(artifact_key))


@router.get('/workspaces')
def file_workspaces(profile: str):
    return guarded(lambda: files.workspaces(profile))


@router.get('/workspace-files')
def workspace_listing(profile: str, workspace_id: str, path: str = '', q: str = '', cursor: str | None = None, limit: int = 50, reveal: bool = False):
    return guarded(lambda: files.listing(profile, workspace_id, path, q, cursor, limit, reveal))


def workspace_target(profile, workspace_id, path, id, reveal):
    files.check_profile(profile)
    if id:
        if workspace_id is not None or path is not None:
            raise HTTPException(400, 'Choose deliverable id or workspace and path')
        return deliverable_store(profile).file_target(id, reveal)
    if workspace_id is None or path is None:
        raise HTTPException(400, 'Workspace and relative path required')
    return workspace_id, path


@router.get('/workspace-files/read')
def workspace_read(profile: str, workspace_id: str | None = None, path: str | None = None, id: str | None = None, reveal: bool = False):
    def run():
        wid, rel = workspace_target(profile, workspace_id, path, id, reveal)
        return files.read_file(profile, wid, rel, reveal, bool(id))
    return guarded(run)


@router.get('/workspace-files/download')
def workspace_download(profile: str, workspace_id: str | None = None, path: str | None = None, id: str | None = None, reveal: bool = False, media_path: str | None = None):
    def run():
        if media_path is not None:
            if any(value is not None for value in (workspace_id,path,id)): raise HTTPException(400,'Choose media path or workspace file')
            return files.transport(profile,None,media_path,media=True)
        wid, rel = workspace_target(profile, workspace_id, path, id, reveal)
        return files.transport(profile, wid, rel, reveal, generation=bool(id))
    return guarded(run)


@router.api_route('/workspace-files/stream', methods=['GET', 'HEAD'])
def workspace_stream(request: Request, profile: str, workspace_id: str | None = None, path: str | None = None, id: str | None = None, reveal: bool = False, media_path: str | None = None):
    def run():
        if media_path is not None:
            if any(value is not None for value in (workspace_id,path,id)): raise HTTPException(400,'Choose media path or workspace file')
            return files.transport(profile,None,media_path,range_header=request.headers.get('range'),head=request.method=='HEAD',stream=True,media=True)
        wid, rel = workspace_target(profile, workspace_id, path, id, reveal)
        return files.transport(profile, wid, rel, reveal, request.headers.get('range'), request.method == 'HEAD', True, bool(id))
    return guarded(run)


@router.get('/session-activity')
def activity(profile: str, stored_session_id: str):
    return guarded(lambda: session_activity(profile, stored_session_id))


@router.get('/session-activity/batch')
def activity_batch(profile: str, stored_session_id: list[str] = Query(...)):
    return guarded(lambda: session_activity_batch(profile, stored_session_id))


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


# Hermex flight recorder: content-free app telemetry, owner-private on this box.
def recorder_profile(body):
    profile = profile_id()
    if body.get('profile') != profile:
        raise HTTPException(403, 'Profile not found')
    return profile


def recorded(fn):
    try:
        return fn()
    except recorder.FlightRecorderConflict:
        return JSONResponse({'error': 'Batch sequence reused with different content', 'code': 'seq_conflict'}, status_code=409)
    except recorder.FlightRecorderFull:
        return JSONResponse({'error': 'Flight recorder storage is full for today', 'code': 'storage_full'}, status_code=507)


def flight_recorder_store():
    return recorder.FlightRecorderStore(recorder.default_root())


@router.post('/flight-recorder/batch')
def flight_recorder_batch(body: dict = Depends(payload)):
    def run():
        profile = recorder_profile(body)
        return recorded(lambda: flight_recorder_store().put_batch(profile, recorder.validate_batch(body)))
    return guarded(run)


@router.post('/flight-recorder/flag')
def flight_recorder_flag(body: dict = Depends(payload)):
    def run():
        profile = recorder_profile(body)
        return recorded(lambda: flight_recorder_store().put_flag(profile, recorder.validate_flag(body)))
    return guarded(run)


@router.post('/flight-recorder/delete')
def flight_recorder_delete(body: dict = Depends(payload)):
    def run():
        recorder_profile(body)
        if set(body) - {'profile', 'install_id'}:
            raise recorder.FlightRecorderError('unknown key')
        return flight_recorder_store().delete_install(recorder.canonical_uuid(body.get('install_id')))
    return guarded(run)
