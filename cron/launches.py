"""Cron creator provenance and exact execution identities share the profile's launch ledger."""
from datetime import datetime


def job_launch_id(job):
    return 'cron-job:' + job['id']


def run_launch_id(execution_id):
    return 'cron-run:' + execution_id


def capture_creator():
    from gateway.session_context import get_session_env
    from agent.subagent_lifecycle import get_active_subagent_parent
    from hermes_constants import get_hermes_home
    agent = get_active_subagent_parent()
    sid = getattr(agent, 'session_id', None) or get_session_env('HERMES_SESSION_ID', '')
    home = get_hermes_home()
    profile = home.name if home.parent.name == 'profiles' else 'default'
    return {'creator_session_id': sid or '', 'creator_profile': profile}


def _epoch(value):
    return datetime.fromisoformat(value).timestamp() if value else None


def sync_jobs(jobs, removed_ids=()):
    from hermes_state_launch_context import launch_writer
    linked = [j for j in jobs if j.get('creator_profile') is not None]
    if not linked and not removed_ids:
        return
    with launch_writer() as db:
        for job in linked:
            lid = job_launch_id(job)
            db.record_launch(lid, kind='check_in', launcher_profile=job['creator_profile'],
                             launcher_session_id=job.get('creator_session_id') or '',
                             child_profile=job['creator_profile'], title=job.get('launch_title'), backend_ref=job['id'])
            db.schedule_launch(lid, _epoch(job.get('next_run_at')))
            if job.get('state') == 'completed':
                db.finish_launch(lid, 'success')
        for jid in removed_ids or ():
            db.finish_launch('cron-job:' + jid, 'user_stop')


def record_run(record, *, creating=False, launcher=None, terminal_cause=None):
    from cron.jobs import get_job
    from hermes_state_launch_context import launch_writer
    job = get_job(record['job_id'])
    cause = terminal_cause or {'completed': 'success', 'failed': 'error', 'unknown': 'unknown'}.get(record['status'])
    if creating and (job is None or job.get('creator_profile') is None):
        return
    if not creating and not cause:
        return
    with launch_writer() as db:
        lid = run_launch_id(record['id'])
        if creating:
            trigger = launcher or (job.get('manual_launch') if job.get('manual_run_at') else None)
            if trigger is None and record['source'] in {'manual', 'direct'}:
                captured = capture_creator()
                trigger = captured if captured['creator_session_id'] else None
            fields = run_fields(job, record['id'])
            if trigger:
                fields.update(launcher_profile=trigger['creator_profile'],
                              launcher_session_id=trigger['creator_session_id'])
            db.record_launch(**fields)
            db.bind_launch(lid, backend_ref=record['id'])
        if cause:
            db.finish_launch(lid, cause)


def run_fields(job, execution_id):
    return dict(launch_id=run_launch_id(execution_id), kind='check_in_run',
                launcher_profile=job['creator_profile'], launcher_session_id=job.get('creator_session_id') or '',
                child_profile=job['creator_profile'], parent_launch_id=job_launch_id(job),
                title=job.get('launch_title'), backend_ref=execution_id)


def bind_run_session(conn, request, session_id):
    from gateway.session_cron import scheduler_execution_id
    from hermes_state_launches import record_launch, bind_launch
    job = request.get('cron_job')
    if not job or job.get('creator_profile') is None:
        return
    eid = scheduler_execution_id(job['id'], request['request_id'])
    fields = run_fields(job, eid)
    if not conn.execute('SELECT 1 FROM work_launches WHERE launch_id=?', (fields['launch_id'],)).fetchone():
        record_launch(conn, **fields)
    bind_launch(conn, fields['launch_id'], child_session_id=session_id, backend_ref=eid)
