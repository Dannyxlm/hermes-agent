"""Reserve a detached background conversation without changing compression/branch lineage."""
from hermes_state_optional_launch import best_effort
from persistence.sqlite.conversation._store.launches import bind_launch, launch_write_patience, record_launch


@best_effort
def reserve_background(db, parent_session_id, child_session_id, title, source='cli'):
    from hermes_state import SessionDB
    from persistence.sqlite.conversation._store.sessions import _own_profile_name
    from persistence.sqlite.conversation._store.worker_lifecycle import insert_session_row_in_transaction
    if db is None or not isinstance(parent_session_id, str):
        return None
    if isinstance(db, SessionDB):
        if db.read_only:
            return None
        # Borrow the caller's existing handle without closing it on return.
        db._enter()
        try:
            return reserve_background(db._store._bound_store().holder(), parent_session_id, child_session_id, title, source)
        finally:
            db._leave()
    lid = 'background:' + child_session_id
    def write(conn):
        parent = conn.execute('SELECT profile_name FROM sessions WHERE id=?', (parent_session_id,)).fetchone()
        profile = (parent[0] if parent else None) or _own_profile_name(db) or 'default'
        record_launch(conn, launch_id=lid, kind='background', launcher_profile=profile,
                      launcher_session_id=parent_session_id, child_profile=profile, title=title,
                      backend_ref=child_session_id)
        insert_session_row_in_transaction(db, conn, child_session_id, source, profile_name=profile)
        bind_launch(conn, lid, child_session_id=child_session_id, backend_ref=child_session_id)
    db._execute_write(write, **launch_write_patience(db))
    return lid


@best_effort
def finish_background(db, launch_id, result=None, *, cause=None):
    if launch_id:
        if cause is None:
            from persistence.sqlite.conversation._store.launch_causes import result_cause
            cause = result_cause(result)
        from hermes_state import SessionDB
        from persistence.sqlite.conversation._store.launches import finish_stored_launch
        if isinstance(db, SessionDB):
            db.finish_launch(launch_id, cause)
        else:
            finish_stored_launch(db, launch_id, cause)
