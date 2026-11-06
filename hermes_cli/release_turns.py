"""Bounded metadata-only release evidence over stdin/stdout JSON.

Invoke the reviewed tree deliberately: python -I -B <tree>/hermes_cli/release_turns.py.
No main CLI, config, credentials, SessionDB facade, migrations, or writers load.

v1 requests require schema_version, operation ('snapshot' or 'lookup'), and an
explicit nonempty profiles=[{profile, home}] roster. Home is an absolute profile
home; only its state.db is read. Lookup additionally requires keys=[{profile,
key:{kind,id,target_session_id,owner_epoch,generation}}]. Nullable fences remain
nullable; no replay/adoption/completion inference is added by this transport.

Success carries raw dataclasses.asdict batches (scope=None means whole profile),
not computed properties. Consumers derive completeness from issues/missing_keys.
Each profile is independently consistent at its first SQLite read; the coverage
bound does NOT certify OS work, future admissions, or a cross-profile instant.
Unexpired leases without ledger evidence are untracked; even a matching root
cannot certify its holder's identity. Both remain incomplete, without PID probes.

Limits: 1 MiB UTF-8 request, 64 profiles, 4096 lookup keys, 4096 characters per
identifier/path, and 8 MiB UTF-8 response. Invalid/oversized transport returns a
coarse structured error and nonzero exit; unavailable databases return incomplete
batches, never new databases. Stdout contains exactly one JSON document; errors
never echo input, paths, exception details, content, or credentials.
"""
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys

# -I deliberately removes cwd/PYTHONPATH. Bind imports to this reviewed tree,
# not whichever installed Hermes or caller's current checkout happens to win.
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_state_release import EvidenceKey, lookup_release_evidence, snapshot_release_evidence


SCHEMA_VERSION = 'hermes-release-evidence.v1'
COVERAGE_BOUND = 'sqlite_ledger_and_turn_leases_at_snapshot'
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PROFILES = 64
MAX_KEYS = 4096
MAX_STRING = 4096
_KEY_FIELDS = {'kind', 'id', 'target_session_id', 'owner_epoch', 'generation'}


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid_request')
    return value


def _string(value):
    if not isinstance(value, str) or not value or len(value) > MAX_STRING or '\x00' in value:
        raise ValueError('invalid_request')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('invalid_request')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('invalid_request')


def _validate(request):
    if not isinstance(request, dict):
        raise ValueError('invalid_request')
    operation = request.get('operation')
    if operation not in ('snapshot', 'lookup'):
        raise ValueError('invalid_request')
    fields = {'schema_version', 'operation', 'profiles'}
    _object(request, fields | ({'keys'} if operation == 'lookup' else set()))
    if request['schema_version'] != SCHEMA_VERSION:
        raise ValueError('invalid_request')
    profiles = request['profiles']
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= MAX_PROFILES:
        raise ValueError('invalid_request')
    homes = {}
    for profile in profiles:
        _object(profile, {'profile', 'home'})
        name = _string(profile['profile'])
        home = _string(profile['home'])
        if (not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name) or name in homes
                or not Path(home).is_absolute()):
            raise ValueError('invalid_request')
        homes[name] = Path(home)
    keys = {name: [] for name in homes}
    if operation == 'lookup':
        requested = request['keys']
        if not isinstance(requested, list) or len(requested) > MAX_KEYS:
            raise ValueError('invalid_request')
        for entry in requested:
            _object(entry, {'profile', 'key'})
            name = _string(entry['profile'])
            if name not in homes:
                raise ValueError('invalid_request')
            raw = _object(entry['key'], _KEY_FIELDS)
            for field in ('kind', 'id', 'target_session_id'):
                _string(raw[field])
            keys[name].append(EvidenceKey(**raw))
    return operation, homes, keys


def _response(request):
    operation, homes, keys = _validate(request)
    profiles = []
    for name, home in homes.items():
        if operation == 'snapshot':
            batch = snapshot_release_evidence(home / 'state.db', scope=None)
        else:
            batch = lookup_release_evidence(home / 'state.db', scope=None, keys=tuple(keys[name]))
        profiles.append({'profile': name, 'batch': asdict(batch)})
    return {'schema_version': SCHEMA_VERSION, 'profiles': profiles, 'coverage_bound': COVERAGE_BOUND}


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('invalid_request')
        request = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
        result = _response(request)
        output = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
        if len(output) > MAX_RESPONSE_BYTES:
            result, code = {'error': {'code': 'response_too_large'}}, 3
        else:
            sys.stdout.buffer.write(output + b'\n')
            return 0
    except (ValueError, TypeError, RecursionError, OverflowError):
        result, code = {'error': {'code': 'invalid_request'}}, 2
    except Exception:
        # Transport-level failure is deliberately coarse. Database/schema failures
        # are normally represented in the actual producer batch's issues instead.
        result, code = {'error': {'code': 'reader_unavailable'}}, 3
    sys.stdout.write(json.dumps({'schema_version': SCHEMA_VERSION, **result}, separators=(',', ':')) + '\n')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
