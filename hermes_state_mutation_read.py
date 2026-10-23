"""Read watermarks on the authoritative mutation's connection, never a second writer."""
import json
import time
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError


def _same_read_owner(conn, anchor, session_id):
    from hermes_state_mutation_binding import (
        _history_sources, _login_label, _owner_user, history_binding, same_history_owner,
    )
    if same_history_owner(conn, anchor['id'], session_id):
        return True
    # Cold-history authorization binds only the requested local row. Its unbound
    # aliases must still match that authenticated owner; this does not claim them
    # or replace the caller's capability/native-transport checks on the anchor.
    binding = history_binding(conn, anchor['id'])
    if (binding is None or anchor['source'] == 'import'
            or history_binding(conn, session_id) is not None):
        return False
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    if row is None or row['session_key'] or row['chat_id'] or row['origin_json']:
        return False
    _profile_id, subject = json.loads(binding)
    return (row['source'] in _history_sources()
            and _owner_user(row) in (None, '', subject, _login_label(subject)))


def set_read_in_transaction(db, conn, session_id, unread):
    from hermes_constants import profile_name_for_home

    family = db._compression_family_on(conn, session_id)
    if not family:
        raise RuntimeStoreError('not_found')
    rows = [conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone() for sid in family]
    anchor = next(row for row in rows if row['id'] == session_id)
    owner = profile_name_for_home(Path(db.db_path).parent)
    profiles = {row['profile_name'] for row in rows if row['profile_name'] is not None}
    if (owner is not None and profiles - {owner}) or len(profiles) > 1:
        raise RuntimeStoreError('profile_mismatch')
    for sid in family:
        if sid != session_id and not _same_read_owner(conn, anchor, sid):
            raise RuntimeStoreError('permission_denied')
    # RETURNING supplies the exact IDs whose revisions the enclosing receipt advances.
    # A failed ownership check or receipt insert rolls back the entire sidebar edit.
    return [row[0] for row in conn.execute(
        f"UPDATE sessions SET last_read_at=? WHERE id IN ({','.join('?' for _ in family)}) RETURNING id",
        (0.0 if unread else time.time(), *family),
    ).fetchall()]
