"""Hermex flight recorder: owner-private, content-free app telemetry on this box.

The phone sends batches of typed events (screens, named taps, RPC/REST timing,
errors, smoothness spans, MetricKit payloads) and, only when Danny taps Send,
bug flags with a screenshot and a note. Everything is validated against a closed
allowlist that mirrors the Swift `FlightRecorderEvent` enum, so free text cannot
ride along in an event. Stores are root-scoped (like the photo catalog), private
(0700/0600), never in Git, and pruned after RETENTION_DAYS.

Layout under ``<Hermes root>/mobile/flight-recorder/``:
  index.sqlite3                      batch/flag dedupe + delete index
  events/YYYY-MM-DD.jsonl            one line per accepted batch (UTC receipt day)
  flags/YYYY-MM-DD/<flag_id>/        flag.json (+ screenshot.jpg)
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import NoReturn

SCHEMA = 1
RETENTION_DAYS = 30
MAX_BATCH_EVENTS = 500
MAX_FLAG_EVENTS = 1000
MAX_NOTE = 2000
MAX_SCREENSHOT = 600 * 1024          # decoded JPEG bytes; fits the 1 MiB body
MAX_METRICKIT = 512 * 1024           # serialized payload bytes
MAX_DAY_FILE = 50 * 1024 * 1024      # per-day events file; beyond this, 507
PRUNE_INTERVAL = 3600.0

_TOKEN = re.compile(r'[A-Za-z0-9_.:-]{1,64}', re.ASCII)
# A Hermes session id (YYYYMMDD_HHMMSS_<hex>) or a UUID, never a title or slug.
_SESSION = re.compile(r'[0-9]{8}_[0-9]{6}_[0-9a-f]{4,32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}',
                      re.ASCII)
_METHOD = re.compile(r'[a-z0-9_.]{1,64}', re.ASCII)
_ROUTE = re.compile(r'/[A-Za-z0-9_./{}-]{0,127}', re.ASCII)
_BUILD = re.compile(r'[0-9]{1,12}', re.ASCII)
_OS = re.compile(r'[0-9.]{1,16}', re.ASCII)
_DEVICE = re.compile(r'[A-Za-z0-9,]{1,32}', re.ASCII)
_DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', re.ASCII)
_MIN_T = 1_577_836_800_000            # 2020-01-01 UTC, epoch milliseconds
_MAX_T = 4_102_444_800_000            # 2100-01-01 UTC

OUTCOMES = frozenset({'ok', 'error', 'cancelled', 'timeout'})
# Closed vocabularies, mirrored from FlightRecorderEvent.swift (round 9 U37, R52-R53). The span
# names, outcomes and attributes are HermexSmoothness's; the nightly report keys budgets on them.
SCREENS = frozenset({'inbox', 'chats', 'chat', 'desktop', 'files', 'file_preview', 'settings', 'sign_in',
                     'flag', 'unknown'})
ERROR_DOMAINS = frozenset({'url', 'posix', 'cocoa', 'transport', 'auth', 'http', 'decoding', 'rpc', 'app'})
SPAN_NAMES = frozenset({'LaunchInboxContent', 'ResumeInboxContent', 'ChatOpenContent', 'ComposerFocusToSettled',
                        'TabFirstContent', 'ScrollInteraction'})
SPAN_OUTCOMES = frozenset({'ok', 'cancelled', 'abandoned', 'superseded', 'empty_verified', 'no_rows', 'no_keyboard'})
SPAN_ATTRS = frozenset({'cached', 'entry', 'from', 'host_ready', 'inbox_rows', 'keyboard_shown',
                        'keyboard_was_visible', 'loading', 'notice', 'pre_main_ms', 'restored', 'rows',
                        'should_begin_calls', 'state', 'surface', 'to', 'verified_empty'})
BG_TRIGGERS = frozenset({'scheduled', 'silent_push'})
BG_OUTCOMES = frozenset({'new_data', 'no_data', 'failed', 'expired'})
PUSH_TYPES = frozenset({'alert', 'background', 'live_activity'})
APP_STATES = frozenset({'active', 'inactive', 'background', 'suspended'})
TAP_SOURCES = frozenset({'banner', 'live_activity', 'widget', 'attention_row', 'snooze_reminder'})
TAP_OUTCOMES = frozenset({'opened', 'foreign_account', 'recoverable', 'rejected'})
LIVE_ACTIONS = frozenset({'start', 'end'})
LIVE_ORIGINS = frozenset({'local', 'push_to_start'})
LIVE_END_STATES = frozenset({'complete', 'failed', 'cancelled', 'stale', 'dismissed'})
LIFECYCLE = frozenset({'launch', 'foreground', 'background', 'terminate_hint', 'memory_warning'})
CONNECTION = frozenset({'connecting', 'connected', 'disconnected', 'failed'})
TRANSPORTS = frozenset({'ws', 'rest'})
HTTP_METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'})
METRICKIT_TYPES = frozenset({'metric', 'diagnostic'})
# MetricKit: the app's fixed top-level keys (HermexMetricKitSubscriber.topLevelKeys); the server
# re-applies the same filtering so an older or modified client cannot store more.
METRICKIT_KEYS = frozenset({
    'timeStampBegin', 'timeStampEnd', 'metaData', 'includesMultipleApplicationVersions', 'latestApplicationVersion',
    'applicationLaunchMetrics', 'applicationResponsivenessMetrics', 'applicationTimeMetrics', 'applicationExitMetrics',
    'cellularConditionMetrics', 'cpuMetrics', 'gpuMetrics', 'diskIOMetrics', 'displayMetrics',
    'locationActivityMetrics', 'memoryMetrics', 'networkTransferMetrics', 'animationMetrics', 'signpostMetrics',
    'crashDiagnostics', 'hangDiagnostics', 'cpuExceptionDiagnostics', 'diskWriteExceptionDiagnostics',
    'appLaunchDiagnostics'})
METRICKIT_SUMMARY_KEYS = frozenset({'hermex_summary', 'original_bytes', 'counts', 'timeStampBegin', 'timeStampEnd',
                                    'metaData', 'includesMultipleApplicationVersions', 'latestApplicationVersion'})
METRICKIT_REMOVED_KEYS = frozenset({'terminationReason', 'virtualMemoryRegionInfo', 'composedMessage', 'formatString',
                                    'arguments', 'exceptionReason', 'regionFormat', 'bundleIdentifier', 'pid'})
METRICKIT_MAX_STRING = 128


class FlightRecorderError(ValueError):
    """Invalid request shape; surfaced as a generic 400 by the router."""


class FlightRecorderConflict(Exception):
    """A batch sequence number was reused with different content."""


class FlightRecorderFull(Exception):
    """Today's store reached its cap."""


# ---------------------------------------------------------------- validation

def _fail(reason) -> NoReturn:
    raise FlightRecorderError(reason)


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail('integer out of range')
    return value


def _num(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        _fail('number out of range')
    return value


def _match(pattern, value):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail('invalid token')
    return value


def _one_of(choices):
    def check(value):
        if value not in choices:
            _fail('invalid enum')
        return value
    return check


def _metrickit_clean(value):
    """Drop free-text keys at any depth and replace long strings, as the app does."""
    if isinstance(value, dict):
        return {k: _metrickit_clean(v) for k, v in value.items() if k not in METRICKIT_REMOVED_KEYS}
    if isinstance(value, list):
        return [_metrickit_clean(v) for v in value]
    if isinstance(value, str) and len(value) > METRICKIT_MAX_STRING:
        return '[removed]'
    return value


def _metrickit_payload(value):
    if not isinstance(value, dict):
        _fail('metrickit payload must be an object')
    if len(json.dumps(value, separators=(',', ':'))) > MAX_METRICKIT:
        _fail('metrickit payload too large')
    if value.get('hermex_summary') is True:
        kept = {k: v for k, v in value.items() if k in METRICKIT_SUMMARY_KEYS and k != 'counts'}
        counts = value.get('counts')
        if isinstance(counts, dict):
            kept['counts'] = {k: n for k, n in counts.items()
                              if k in METRICKIT_KEYS and type(n) is int and 0 <= n <= 2**31}
    else:
        kept = {k: v for k, v in value.items() if k in METRICKIT_KEYS}
    return _metrickit_clean(kept)


def _span_attrs(value):
    if not isinstance(value, dict) or set(value) - SPAN_ATTRS:
        _fail('invalid span attributes')
    for attr in value.values():
        _match(_TOKEN, attr)
    return value


_ms = lambda v: _num(v, 0, 3_600_000)
_bytes = lambda v: _int(v, 0, 2**31)
_code = lambda v: _int(v, -2**31, 2**31)
_token = lambda v: _match(_TOKEN, v)
_session = lambda v: _match(_SESSION, v)

# kind -> (required keys, optional keys); every value has a validator.
# Mirror of HermesMobile/Diagnostics/FlightRecorderEvent.swift. Adding a key on
# one side without the other must fail closed (unknown keys are rejected).
KINDS = {
    'lifecycle': ({'state': _one_of(LIFECYCLE)}, {}),
    'screen': ({'screen': _one_of(SCREENS)}, {'session_id': _session}),
    'tap': ({'control': _token, 'screen': _one_of(SCREENS)}, {'session_id': _session}),
    'rpc': ({'method': lambda v: _match(_METHOD, v), 'outcome': _one_of(OUTCOMES), 'ms': _ms},
            {'bytes': _bytes, 'code': _code, 'session_id': _session}),
    'http': ({'route': lambda v: _match(_ROUTE, v), 'verb': _one_of(HTTP_METHODS),
              'outcome': _one_of(OUTCOMES), 'ms': _ms},
             {'status': lambda v: _int(v, 0, 999), 'bytes': _bytes, 'session_id': _session}),
    'error': ({'domain': _one_of(ERROR_DOMAINS), 'code': _code},
              {'screen': _one_of(SCREENS), 'context': _token, 'session_id': _session}),
    'span': ({'name': _one_of(SPAN_NAMES), 'outcome': _one_of(SPAN_OUTCOMES), 'ms': _ms},
             {'screen': _one_of(SCREENS), 'session_id': _session, 'attrs': _span_attrs}),
    'connection': ({'state': _one_of(CONNECTION), 'transport': _one_of(TRANSPORTS)},
                   {'code': _code}),
    'metrickit': ({'payload_type': _one_of(METRICKIT_TYPES), 'payload': _metrickit_payload}, {}),
    # R53: lane A and E lifecycle events, so they can be verified on the phone.
    'bg_refresh': ({'trigger': _one_of(BG_TRIGGERS), 'outcome': _one_of(BG_OUTCOMES), 'ms': _ms}, {}),
    'push': ({'push_type': _one_of(PUSH_TYPES), 'app_state': _one_of(APP_STATES)}, {'session_id': _session}),
    'push_tap': ({'source': _one_of(TAP_SOURCES), 'outcome': _one_of(TAP_OUTCOMES)}, {'session_id': _session}),
    'live_activity': ({'action': _one_of(LIVE_ACTIONS), 'origin': _one_of(LIVE_ORIGINS)},
                      {'end_state': _one_of(LIVE_END_STATES), 'session_id': _session}),
}


def validate_event(event):
    if not isinstance(event, dict):
        _fail('event must be an object')
    kind = event.get('kind')
    if not isinstance(kind, str) or kind not in KINDS:
        _fail('unknown event kind')
    required, optional = KINDS[kind]
    unknown = set(event) - {'kind', 't'} - set(required) - set(optional)
    if unknown:
        _fail('unknown event key')
    _int(event.get('t'), _MIN_T, _MAX_T)
    # Validators return the value to store (MetricKit payloads come back filtered).
    for key, check in required.items():
        if key not in event:
            _fail('missing event key')
        event[key] = check(event[key])
    for key, check in optional.items():
        if key in event and event[key] is not None:
            event[key] = check(event[key])
    return event


def canonical_uuid(value):
    if not isinstance(value, str):
        _fail('uuid required')
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        _fail('uuid required')
    # Accept Swift's upper-case uuidString; store the lower-case canonical form.
    if str(parsed) != value.lower():
        _fail('canonical uuid required')
    return str(parsed)


def _envelope(body, keys):
    allowed = {'profile', 'schema', 'install_id', 'app_build', 'os_version', 'device_class', 'events'} | keys
    if set(body) - allowed:
        _fail('unknown envelope key')
    if body.get('schema') != SCHEMA:
        _fail('unsupported schema')
    return {
        'install_id': canonical_uuid(body.get('install_id')),
        'app_build': _match(_BUILD, body.get('app_build')),
        'os_version': _match(_OS, body.get('os_version')),
        'device_class': _match(_DEVICE, body.get('device_class')),
    }


def validate_batch(body):
    meta = _envelope(body, {'batch_seq'})
    meta['batch_seq'] = _int(body.get('batch_seq'), 0, 2**53)
    events = body.get('events')
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH_EVENTS:
        _fail('events must be a non-empty bounded list')
    meta['events'] = [validate_event(e) for e in events]
    return meta


def validate_flag(body):
    meta = _envelope(body, {'flag_id', 't', 'screen', 'note', 'screenshot_jpeg_b64'})
    meta['flag_id'] = canonical_uuid(body.get('flag_id'))
    meta['t'] = _int(body.get('t'), _MIN_T, _MAX_T)
    meta['screen'] = _one_of(SCREENS)(body.get('screen'))
    note = body.get('note', '')
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        _fail('note too long')
    meta['note'] = note
    events = body.get('events', [])
    if not isinstance(events, list) or len(events) > MAX_FLAG_EVENTS:
        _fail('flag events must be a bounded list')
    meta['events'] = [validate_event(e) for e in events]
    shot = body.get('screenshot_jpeg_b64')
    meta['screenshot'] = None
    if shot is not None:
        if not isinstance(shot, str) or len(shot) > (MAX_SCREENSHOT * 4) // 3 + 4:
            _fail('screenshot too large')
        try:
            data = base64.b64decode(shot, validate=True)
        except (binascii.Error, ValueError):
            _fail('screenshot must be base64')
        if len(data) > MAX_SCREENSHOT or not data.startswith(b'\xff\xd8\xff'):
            _fail('screenshot must be a bounded JPEG')
        meta['screenshot'] = data
    return meta


# ------------------------------------------------------------------- storage

def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _receipt(*parts):
    return hashlib.sha256(':'.join(str(p) for p in parts).encode()).hexdigest()[:24]


def _private_dir(path: Path):
    """Create (0700) or accept an existing real directory; never follow a symlink."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise OSError(errno.ELOOP, 'flight recorder path is not a private directory', str(path))
    return path


def _write_private(path: Path, data: bytes, append=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


_prune_lock = threading.Lock()
_last_prune = {}


class FlightRecorderStore:
    def __init__(self, root: Path, now=None):
        self.root = Path(root)
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    # Layout ----------------------------------------------------------------
    def _ensure(self):
        _private_dir(self.root.parent)
        _private_dir(self.root)
        _private_dir(self.root / 'events')
        _private_dir(self.root / 'flags')

    def connection(self):
        self._ensure()
        db = sqlite3.connect(self.root / 'index.sqlite3', timeout=10, isolation_level=None)
        os.chmod(self.root / 'index.sqlite3', 0o600)
        db.execute('CREATE TABLE IF NOT EXISTS batches (install_id TEXT NOT NULL, batch_seq INTEGER NOT NULL, '
                   'digest TEXT NOT NULL, day TEXT NOT NULL, received_at TEXT NOT NULL, '
                   'PRIMARY KEY (install_id, batch_seq))')
        db.execute('CREATE TABLE IF NOT EXISTS flags (flag_id TEXT PRIMARY KEY, install_id TEXT NOT NULL, '
                   'digest TEXT NOT NULL, day TEXT NOT NULL, received_at TEXT NOT NULL)')
        return db

    # Writes ----------------------------------------------------------------
    def put_batch(self, profile, meta):
        now = self._now()
        day, received = now.strftime('%Y-%m-%d'), now.isoformat(timespec='seconds')
        record = {'received_at': received, 'profile': profile, **meta}
        digest = _digest({k: v for k, v in record.items() if k not in ('received_at',)})
        receipt = _receipt('batch', meta['install_id'], meta['batch_seq'])
        line = (json.dumps(record, separators=(',', ':')) + '\n').encode()
        db = self.connection()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest FROM batches WHERE install_id=? AND batch_seq=?',
                             (meta['install_id'], meta['batch_seq'])).fetchone()
            if row:
                db.execute('ROLLBACK')
                if row[0] != digest:
                    raise FlightRecorderConflict()
                return {'ok': True, 'receipt_id': receipt, 'accepted': 0, 'duplicate': True}
            path = self.root / 'events' / f'{day}.jsonl'
            try:
                size = os.lstat(path).st_size
            except FileNotFoundError:
                size = 0
            if size + len(line) > MAX_DAY_FILE:
                db.execute('ROLLBACK')
                raise FlightRecorderFull()
            _write_private(path, line, append=True)
            db.execute('INSERT INTO batches VALUES (?,?,?,?,?)',
                       (meta['install_id'], meta['batch_seq'], digest, day, received))
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()
        self.maybe_prune()
        return {'ok': True, 'receipt_id': receipt, 'accepted': len(meta['events']), 'duplicate': False}

    def put_flag(self, profile, meta):
        now = self._now()
        day, received = now.strftime('%Y-%m-%d'), now.isoformat(timespec='seconds')
        shot = meta.pop('screenshot')
        record = {'received_at': received, 'profile': profile, 'has_screenshot': shot is not None, **meta}
        digest = _digest({k: v for k, v in record.items() if k != 'received_at'} |
                         {'screenshot_sha256': hashlib.sha256(shot).hexdigest() if shot else None})
        receipt = _receipt('flag', meta['flag_id'])
        db = self.connection()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest FROM flags WHERE flag_id=?', (meta['flag_id'],)).fetchone()
            if row:
                db.execute('ROLLBACK')
                if row[0] != digest:
                    raise FlightRecorderConflict()
                return {'ok': True, 'receipt_id': receipt, 'duplicate': True}
            folder = _private_dir(_private_dir(self.root / 'flags' / day) / meta['flag_id'])
            if shot is not None:
                _write_private(folder / 'screenshot.jpg', shot)
            _write_private(folder / 'flag.json', json.dumps(record, separators=(',', ':')).encode())
            db.execute('INSERT INTO flags VALUES (?,?,?,?,?)',
                       (meta['flag_id'], meta['install_id'], digest, day, received))
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()
        self.maybe_prune()
        return {'ok': True, 'receipt_id': receipt, 'duplicate': False}

    # Deletion and retention -------------------------------------------------
    def delete_install(self, install_id):
        """Remove every batch line and flag stored for one installation."""
        removed_batches = removed_flags = 0
        db = self.connection()
        try:
            db.execute('BEGIN IMMEDIATE')
            days = [r[0] for r in db.execute('SELECT DISTINCT day FROM batches WHERE install_id=?', (install_id,))]
            for day in days:
                path = self.root / 'events' / f'{day}.jsonl'
                if not path.exists() or path.is_symlink():
                    continue
                kept, dropped = [], 0
                with open(path, 'rb') as handle:
                    for line in handle:
                        try:
                            mine = json.loads(line).get('install_id') == install_id
                        except ValueError:
                            mine = False
                        if mine:
                            dropped += 1
                        else:
                            kept.append(line)
                removed_batches += dropped
                tmp = path.with_suffix('.jsonl.tmp')
                if tmp.exists():
                    tmp.unlink()
                _write_private(tmp, b''.join(kept))
                os.replace(tmp, path)
            for flag_id, day in db.execute('SELECT flag_id, day FROM flags WHERE install_id=?', (install_id,)).fetchall():
                folder = self.root / 'flags' / day / flag_id
                if folder.is_dir() and not folder.is_symlink():
                    shutil.rmtree(folder)
                    removed_flags += 1
            db.execute('DELETE FROM batches WHERE install_id=?', (install_id,))
            db.execute('DELETE FROM flags WHERE install_id=?', (install_id,))
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()
        return {'ok': True, 'removed_batches': removed_batches, 'removed_flags': removed_flags}

    def maybe_prune(self):
        key = str(self.root)
        with _prune_lock:
            if time.monotonic() - _last_prune.get(key, -PRUNE_INTERVAL * 2) < PRUNE_INTERVAL:
                return None
            _last_prune[key] = time.monotonic()
        return self.prune()

    def prune(self):
        """Delete day files and flag folders older than RETENTION_DAYS; names only."""
        cutoff = (self._now() - dt.timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%d')
        removed = 0
        for entry in (self.root / 'events').iterdir():
            day = entry.name[:-len('.jsonl')] if entry.name.endswith('.jsonl') else None
            if day and _DAY.fullmatch(day) and day < cutoff and entry.is_file() and not entry.is_symlink():
                entry.unlink()
                removed += 1
        for entry in (self.root / 'flags').iterdir():
            if _DAY.fullmatch(entry.name) and entry.name < cutoff and entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
                removed += 1
        db = self.connection()
        try:
            db.execute('DELETE FROM batches WHERE day < ?', (cutoff,))
            db.execute('DELETE FROM flags WHERE day < ?', (cutoff,))
        finally:
            db.close()
        return removed


def default_root():
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / 'mobile' / 'flight-recorder'
