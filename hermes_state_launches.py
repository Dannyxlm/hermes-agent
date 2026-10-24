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
    payload['batch_name'] = short_title(payload['batch_name'])
    row = conn.execute('SELECT * FROM work_launches WHERE launch_id=?', (payload['launch_id'],)).fetchone()
    if row is not None:
        if any(row[k] != v for k, v in payload.items() if k != 'next_run_at'):
            raise ValueError('launch identity conflict')
        resume_launch(conn, payload['launch_id'])
        return payload['launch_id']
    fields = list(payload) + ['created_at']
    conn.execute(f"INSERT INTO work_launches ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                 [*payload.values(), time.time()])
    return payload['launch_id']


def resume_launch(conn, launch_id):
    conn.execute("UPDATE work_launches SET finished_at=NULL,terminal_cause=NULL "
                 "WHERE launch_id=? AND terminal_cause='owner_restart'", (launch_id,))


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
    resume_launch(conn, launch_id)


def finish_launch(conn, launch_id, terminal_cause):
    if terminal_cause not in TERMINAL_CAUSES:
        raise ValueError('invalid terminal cause')
    conn.execute('UPDATE work_launches SET finished_at=COALESCE(finished_at,?),terminal_cause=? '
                 "WHERE launch_id=? AND (finished_at IS NULL OR (terminal_cause='unknown' AND ? != 'unknown'))",
                 (time.time(), terminal_cause, launch_id, terminal_cause))


def sync_job_launches(conn, jobs, removed_ids):
    from datetime import datetime
    schedules = []
    for job in jobs:
        lid = 'cron-job:' + job['id']
        if not conn.execute('SELECT 1 FROM work_launches WHERE launch_id=?', (lid,)).fetchone():
            record_launch(conn, launch_id=lid, kind='check_in', launcher_profile=job['creator_profile'],
                          launcher_session_id=job.get('creator_session_id') or '', child_profile=job['creator_profile'],
                          title=job.get('launch_title'), backend_ref=job['id'])
        schedules.append((datetime.fromisoformat(job['next_run_at']).timestamp() if job.get('next_run_at') else None, lid))
        if job.get('state') == 'completed':
            finish_launch(conn, lid, 'success')
    conn.executemany('UPDATE work_launches SET next_run_at=? WHERE launch_id=?', schedules)
    for jid in removed_ids:
        finish_launch(conn, 'cron-job:' + jid, 'user_stop')


class SessionLaunchesMixin:
    def sync_launch_jobs(self, jobs, removed_ids):
        return self._execute_write(lambda conn: sync_job_launches(conn, jobs, removed_ids), patience_s=0)

    def record_launch(self, launch_id, *, kind, launcher_profile, launcher_session_id, child_profile,
                      parent_launch_id=None, batch_id=None, batch_name=None, title=None, backend_ref=None,
                      next_run_at=None):
        payload = dict(launch_id=launch_id, kind=kind, launcher_profile=launcher_profile,
                       launcher_session_id=launcher_session_id, child_profile=child_profile,
                       parent_launch_id=parent_launch_id, batch_id=batch_id, batch_name=batch_name,
                       title=title, backend_ref=backend_ref, next_run_at=next_run_at)
        return self._execute_write(lambda conn: record_launch(conn, **payload), patience_s=0)

    def bind_launch(self, launch_id, *, child_session_id=None, backend_ref=None):
        return self._execute_write(lambda conn: bind_launch(conn, launch_id,
                                  child_session_id=child_session_id, backend_ref=backend_ref), patience_s=0)

    def finish_launch(self, launch_id, terminal_cause):
        return self._execute_write(lambda conn: finish_launch(conn, launch_id, terminal_cause), patience_s=0)

    def schedule_launch(self, launch_id, next_run_at):
        if next_run_at is not None and (type(next_run_at) not in (float, int) or not math.isfinite(next_run_at)):
            raise ValueError('invalid next_run_at')
        self._write_sql('UPDATE work_launches SET next_run_at=? WHERE launch_id=?', (next_run_at, launch_id), patience_s=0)


def worker_launch(db, conn, session_id, payload, *, operation):
    from hermes_state_runtime import RuntimeStoreError
    try:
        profile = conn.execute('SELECT profile_name FROM sessions WHERE id=?', (session_id,)).fetchone()[0]
        profile = profile or db._own_profile_name() or 'default'
        from hermes_state_runtime_workers import runtime_lineage
        scope = runtime_lineage(conn, session_id)
        if operation == 'record':
            validate_record(payload)
            shared_job = payload['kind'] == 'check_in' and conn.execute(
                "SELECT 1 FROM work_launches WHERE launch_id=? AND kind='check_in' AND child_profile=?",
                (payload['launch_id'], profile)).fetchone()
            if (payload['child_profile'] != profile or (not shared_job and (
                    payload['launcher_session_id'] not in scope or payload['launcher_profile'] != profile))):
                raise RuntimeStoreError('permission_denied')
            return {'value': record_launch(conn, **payload)}
        fields = {'bind': {'launch_id', 'child_session_id', 'backend_ref'},
                  'finish': {'launch_id', 'terminal_cause'}, 'schedule': {'launch_id', 'next_run_at'}}[operation]
        if set(payload) != fields or not isinstance(payload['launch_id'], str):
            raise ValueError('invalid launch fields')
        row = conn.execute('SELECT * FROM work_launches WHERE launch_id=?', (payload['launch_id'],)).fetchone()
        if row is None and operation == 'finish':
            finish_launch(conn, **payload)
            return {'value': None}
        shared_job = (row is not None and operation in {'schedule', 'finish'} and
                      row['kind'] == 'check_in' and row['child_profile'] == profile)
        if row is None or (not shared_job and not scope.intersection((row['launcher_session_id'], row['child_session_id']))):
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
    except RuntimeStoreError:
        raise
    except (ValueError, TypeError) as exc:
        raise RuntimeStoreError('invalid_params') from exc



def mutate_optional_worker_launch(db, *, epoch, execution_id, session_id, generation, operation, payload):
    from hermes_state_runtime import _epoch, _worker_assignment, RuntimeStoreError
    def write(conn):
        _epoch(conn, epoch)
        row = _worker_assignment(conn, execution_id, session_id, generation)
        if row['owner_epoch'] != epoch or row['status'] not in ('registered', 'running'):
            raise RuntimeStoreError('stale_generation')
        if operation == 'launch.sync':
            if set(payload) != {'jobs', 'removed_ids'}:
                raise RuntimeStoreError('invalid_params')
            for job in payload['jobs']:
                worker_launch(db, conn, session_id, dict(launch_id='cron-job:' + job['id'], kind='check_in',
                    launcher_profile=job['creator_profile'], launcher_session_id=job.get('creator_session_id') or '',
                    child_profile=job['creator_profile'], title=job.get('launch_title'), backend_ref=job['id']), operation='record')
            for jid in payload['removed_ids']:
                worker_launch(db, conn, session_id, dict(launch_id='cron-job:' + jid, terminal_cause='user_stop'), operation='finish')
            sync_job_launches(conn, payload['jobs'], [])
            return {'value': None}
        if operation not in {'launch.record', 'launch.bind', 'launch.finish', 'launch.schedule'}:
            raise RuntimeStoreError('invalid_params')
        return worker_launch(db, conn, session_id, payload, operation=operation.split('.')[1])
    return db._execute_write(write, patience_s=0)
