"""Reserve a detached background conversation without changing compression/branch lineage."""
from hermes_state_optional_launch import best_effort
from hermes_state_launches import record_launch, bind_launch


@best_effort
def reserve_background(db, parent_session_id, child_session_id, title, source='cli'):
    from hermes_state import SessionDB
    from hermes_state_worker_lifecycle import insert_session_row_in_transaction
    if not isinstance(db, SessionDB) or not isinstance(parent_session_id, str):
        return None
    lid = 'background:' + child_session_id
    def write(conn):
        parent = conn.execute('SELECT profile_name FROM sessions WHERE id=?', (parent_session_id,)).fetchone()
        profile = (parent[0] if parent else None) or db._own_profile_name() or 'default'
        record_launch(conn, launch_id=lid, kind='background', launcher_profile=profile,
                      launcher_session_id=parent_session_id, child_profile=profile, title=title,
                      backend_ref=child_session_id)
        insert_session_row_in_transaction(db, conn, child_session_id, source, profile_name=profile)
        bind_launch(conn, lid, child_session_id=child_session_id, backend_ref=child_session_id)
    db._execute_write(write, patience_s=0)
    return lid


@best_effort
def finish_background(db, launch_id, result=None, *, cause=None):
    if launch_id:
        if cause is None:
            from hermes_state_launch_causes import result_cause
            cause = result_cause(result)
        db.finish_launch(launch_id, cause)
