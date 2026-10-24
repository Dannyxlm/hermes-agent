"""Consume one process launch's provenance before profile switching or tool environment capture."""
from hermes_state_optional_launch import best_effort
import os
from uuid import uuid4

ENV_FIELDS = {'HERMES_STARTED_BY_SESSION_ID': 'started_by_session',
              'HERMES_STARTED_BY_PROFILE': 'started_by_profile', 'HERMES_LAUNCH_TITLE': 'launch_title',
              'HERMES_LAUNCH_KIND': 'launch_kind', 'HERMES_LAUNCH_ID': 'launch_id'}
FIELDS = frozenset(ENV_FIELDS.values())
_pending = {}


def capture_startup():
    global _pending
    values = {field: os.environ.pop(env) for env, field in ENV_FIELDS.items() if env in os.environ}
    _pending.update(values)


def prepare_launch(args):
    capture_startup()
    _pending.update({key: value for key in FIELDS if (value := getattr(args, key, None)) is not None})


def take_launch(args=None):
    prepare_launch(args)
    fields = dict(_pending)
    _pending.clear()
    validate_intake(fields)
    if fields.get('started_by_session'):
        fields.setdefault('launch_id', str(uuid4()))
    return fields


def validate_intake(fields):
    from hermes_state_optional_launch import warn
    invalid = {k for k, v in fields.items() if k in FIELDS and (not isinstance(v, str) or not v.strip())}
    if fields.get('launch_kind') is not None and ('launch_kind' in invalid or fields['launch_kind'] not in {'lane', 'teammate'}):
        invalid.add('launch_kind')
    for key in invalid:
        fields.pop(key, None)
    if fields.get('started_by_session') and not fields.get('started_by_profile'):
        fields.pop('started_by_session')
        invalid.add('started_by_session')
    if invalid:
        warn('invalid provenance', ValueError())

def launch_record(fields, child_profile, *, launch_id=None):
    fields = dict(fields)
    validate_intake(fields)
    if not fields.get('started_by_session'):
        return None
    launcher = fields['started_by_profile']
    return dict(launch_id=fields.get('launch_id') or launch_id or str(uuid4()),
                kind=fields.get('launch_kind') or ('teammate' if launcher != child_profile else 'lane'),
                launcher_profile=launcher, launcher_session_id=fields['started_by_session'],
                child_profile=child_profile, title=fields.get('launch_title'))


@best_effort
def record_for_db(db, fields):
    if db is None:
        return None
    record = launch_record(fields, db._own_profile_name() or 'default')
    return db.record_launch(**record) if record else None
