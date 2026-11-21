"""Launch reservation shares the transaction that creates a delegated session."""
from uuid import uuid4


def reserve_local_child(store, parent_session_id, metadata):
    from hermes_state_optional_launch import optional_launch
    from persistence_assembly import Persistence
    from persistence.sqlite.conversation._store.launches import bind_launch, launch_write_patience, record_launch
    from persistence.sqlite.conversation._store.worker_lifecycle import insert_session_row_in_transaction
    from persistence.sqlite.conversation._store.sessions import _own_profile_name
    from session_rules.session_ids import new_session_id

    if not isinstance(store, Persistence) or not isinstance(parent_session_id, str):
        return None
    holder = store._bound_store().holder()
    child_sid = new_session_id()

    def reserve(conn):
        parent = conn.execute("SELECT profile_name FROM sessions WHERE id=?", (parent_session_id,)).fetchone()
        profile = parent[0] if parent and parent[0] else _own_profile_name(holder)
        record_launch(conn, **metadata, kind="helper", launcher_profile=profile,
                      launcher_session_id=parent_session_id, child_profile=profile)
        insert_session_row_in_transaction(holder, conn, child_sid, "subagent",
                                          model_config={"_delegate_from": parent_session_id},
                                          parent_session_id=parent_session_id, profile_name=profile)
        bind_launch(conn, metadata["launch_id"], child_session_id=child_sid)

    with optional_launch("delegate.reserve"):
        holder._execute_write(reserve, **launch_write_patience(holder))
        return child_sid
    return None


def launch_metadata(*, batch_id=None, batch_name=None, title=None):
    from persistence.sqlite.conversation._store.launches import short_title
    return dict(launch_id=str(uuid4()), batch_id=batch_id or str(uuid4()),
                batch_name=short_title(batch_name), title=short_title(title))
