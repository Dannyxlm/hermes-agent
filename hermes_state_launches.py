"""Profile-local launch provenance. It never grants access or changes session lineage."""
import math
import time

KINDS = frozenset({'helper', 'lane', 'teammate', 'check_in', 'check_in_run', 'process', 'background'})
TERMINAL_CAUSES = frozenset({'success', 'error', 'user_stop', 'parent_stop', 'owner_restart', 'failed_start', 'unknown'})
RECORD_FIELDS = frozenset({'launch_id', 'kind', 'launcher_profile', 'launcher_session_id', 'child_profile',
                           'parent_launch_id', 'batch_id', 'batch_name', 'title', 'backend_ref', 'next_run_at'})


def short_title(title):
    return title if title is None or len(title) <= 40 else title[:39] + '…'


def validate_record(payload):
    required = {'launch_id', 'kind', 'launcher_profile', 'launcher_session_id', 'child_profile'}
    if (set(payload) - RECORD_FIELDS or not required <= set(payload)
            or any(not isinstance(payload[k], str) or (not payload[k] and not (
                k == 'launcher_session_id')) for k in required)
            or payload['kind'] not in KINDS
            or any(v is not None and not isinstance(v, str) for k, v in payload.items() if k != 'next_run_at')):
        raise ValueError('invalid launch')
    when = payload.get('next_run_at')
    if when is not None and (type(when) not in (float, int) or not math.isfinite(when)):
        raise ValueError('invalid next_run_at')


def record_launch(conn, **payload):
    validate_record(payload)
    payload = {key: payload.get(key) for key in sorted(RECORD_FIELDS)}
    payload['title'] = short_title(payload['title'])
    row = conn.execute('SELECT * FROM work_launches WHERE launch_id=?', (payload['launch_id'],)).fetchone()
    if row is not None:
        if any(row[k] != v for k, v in payload.items() if k != 'next_run_at'):
            raise ValueError('launch identity conflict')
        return payload['launch_id']
    fields = list(payload) + ['created_at']
    conn.execute(f"INSERT INTO work_launches ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                 [*payload.values(), time.time()])
    return payload['launch_id']


def bind_launch(conn, launch_id, *, child_session_id=None, backend_ref=None):
    row = conn.execute('SELECT * FROM work_launches WHERE launch_id=?', (launch_id,)).fetchone()
    if row is None:
        raise ValueError('unknown launch')
    for key, value in (('child_session_id', child_session_id), ('backend_ref', backend_ref)):
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError('launch binding conflict')
        if value is not None and row[key] not in (None, value):
            from hermes_state_sessions import _expand_compression_lineage_ids
            if key != 'child_session_id' or row[key] not in _expand_compression_lineage_ids(conn, [value]):
                raise ValueError('launch binding conflict')
    conn.execute('UPDATE work_launches SET child_session_id=COALESCE(child_session_id,?), '
                 'backend_ref=COALESCE(backend_ref,?),bound_at=COALESCE(bound_at,?) WHERE launch_id=?',
                 (child_session_id, backend_ref, time.time(), launch_id))


def finish_launch(conn, launch_id, terminal_cause):
    if terminal_cause not in TERMINAL_CAUSES:
        raise ValueError('invalid terminal cause')
    conn.execute('UPDATE work_launches SET finished_at=COALESCE(finished_at,?),terminal_cause=? '
                 "WHERE launch_id=? AND (finished_at IS NULL OR (terminal_cause='unknown' AND ? != 'unknown'))",
                 (time.time(), terminal_cause, launch_id, terminal_cause))


class SessionLaunchesMixin:
    def record_launch(self, launch_id, *, kind, launcher_profile, launcher_session_id, child_profile,
                      parent_launch_id=None, batch_id=None, batch_name=None, title=None, backend_ref=None,
                      next_run_at=None):
        payload = dict(launch_id=launch_id, kind=kind, launcher_profile=launcher_profile,
                       launcher_session_id=launcher_session_id, child_profile=child_profile,
                       parent_launch_id=parent_launch_id, batch_id=batch_id, batch_name=batch_name,
                       title=title, backend_ref=backend_ref, next_run_at=next_run_at)
        return self._execute_write(lambda conn: record_launch(conn, **payload))

    def bind_launch(self, launch_id, *, child_session_id=None, backend_ref=None):
        return self._execute_write(lambda conn: bind_launch(conn, launch_id,
                                  child_session_id=child_session_id, backend_ref=backend_ref))

    def finish_launch(self, launch_id, terminal_cause):
        return self._execute_write(lambda conn: finish_launch(conn, launch_id, terminal_cause))

    def schedule_launch(self, launch_id, next_run_at):
        if next_run_at is not None and (type(next_run_at) not in (float, int) or not math.isfinite(next_run_at)):
            raise ValueError('invalid next_run_at')
        self._write_sql('UPDATE work_launches SET next_run_at=? WHERE launch_id=?', (next_run_at, launch_id))


def worker_launch(db, conn, session_id, payload, *, operation):
    from hermes_state_runtime import RuntimeStoreError
    try:
        if operation == 'record':
            validate_record(payload)
            profile = conn.execute('SELECT profile_name FROM sessions WHERE id=?', (session_id,)).fetchone()[0]
            if (payload['launcher_session_id'] != session_id or payload['launcher_profile'] != profile
                    or payload['child_profile'] != profile):
                raise RuntimeStoreError('permission_denied')
            return {'value': record_launch(conn, **payload)}
        fields = {'bind': {'launch_id', 'child_session_id', 'backend_ref'},
                  'finish': {'launch_id', 'terminal_cause'}, 'schedule': {'launch_id', 'next_run_at'}}[operation]
        if set(payload) != fields or not isinstance(payload['launch_id'], str):
            raise ValueError('invalid launch fields')
        row = conn.execute('SELECT * FROM work_launches WHERE launch_id=?', (payload['launch_id'],)).fetchone()
        from hermes_state_runtime_workers import runtime_lineage
        scope = runtime_lineage(conn, session_id)
        if row is None or not scope.intersection((row['launcher_session_id'], row['child_session_id'])):
            raise RuntimeStoreError('permission_denied')
        if operation == 'schedule':
            when = payload['next_run_at']
            if when is not None and (type(when) not in (float, int) or not math.isfinite(when)):
                raise ValueError('invalid next_run_at')
            conn.execute('UPDATE work_launches SET next_run_at=? WHERE launch_id=?', (when, payload['launch_id']))
        elif operation == 'bind':
            # Child reservation, not provenance, is the authority to bind another session.
            if payload['child_session_id'] not in (None, session_id, row['child_session_id']):
                raise RuntimeStoreError('permission_denied')
            bind_launch(conn, **payload)
        else:
            finish_launch(conn, **payload)
        return {'value': None}
    except (ValueError, TypeError) as exc:
        raise RuntimeStoreError('invalid_params') from exc
