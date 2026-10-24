"""Launch persistence for terminal process producers and their retained completion receipts."""
from pathlib import Path


def fields(session):
    return {k: getattr(session, k) for k in ('batch_id', 'batch_name', 'title')}


def record_process_launch(session):
    from hermes_state_launch_context import launch_writer
    with launch_writer() as db:
        db.record_launch(session.launch_id, kind='process', launcher_profile=session.launcher_profile,
                         launcher_session_id=session.launcher_session_id, child_profile=session.launcher_profile,
                         title=session.title, batch_id=session.batch_id, batch_name=session.batch_name,
                         backend_ref=session.id)
        db.bind_launch(session.launch_id, backend_ref=session.id)


def finish_process_launch(session, cause=None):
    if not session.launch_id:
        return
    from agent.runtime_session_store import is_worker_process
    if cause is None:
        cause = ('success' if session.exit_code == 0 else 'error') if session.exit_code is not None else 'unknown'
    if is_worker_process():
        from hermes_state_launch_context import launch_writer
        with launch_writer() as db:
            db.finish_launch(session.launch_id, cause)
    else:
        from hermes_state_registry import acquire, release_or_close
        if not session.owner_profile_home:
            return
        db = acquire(Path(session.owner_profile_home) / 'state.db')
        try:
            db.finish_launch(session.launch_id, cause)
        finally:
            release_or_close(db)
