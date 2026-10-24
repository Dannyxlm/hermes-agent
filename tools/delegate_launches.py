"""Launch reservation shares the transaction that creates a delegated session."""
from hermes_state_optional_launch import best_effort
from uuid import uuid4


@best_effort
def reserve_local_child(db, parent_session_id, metadata):
    from hermes_state import SessionDB
    from hermes_state_ids import new_session_id
    from hermes_state_launches import record_launch, bind_launch
    from hermes_state_worker_lifecycle import insert_session_row_in_transaction
    if not isinstance(db, SessionDB) or not isinstance(parent_session_id, str):
        return None
    child_sid = new_session_id()

    def reserve(conn):
        parent = conn.execute('SELECT profile_name FROM sessions WHERE id=?', (parent_session_id,)).fetchone()
        profile = parent[0] if parent and parent[0] else db._own_profile_name()
        record_launch(conn, **metadata, kind='helper', launcher_profile=profile,
                      launcher_session_id=parent_session_id, child_profile=profile)
        insert_session_row_in_transaction(db, conn, child_sid, 'subagent',
                                          model_config={'_delegate_from': parent_session_id},
                                          parent_session_id=parent_session_id, profile_name=profile)
        bind_launch(conn, metadata['launch_id'], child_session_id=child_sid)
    db._execute_write(reserve, patience_s=0)
    return child_sid


def launch_metadata(*, batch_id=None, batch_name=None, title=None):
    from hermes_state_launches import short_title
    return dict(launch_id=str(uuid4()), batch_id=batch_id or str(uuid4()),
                batch_name=short_title(batch_name), title=short_title(title))
