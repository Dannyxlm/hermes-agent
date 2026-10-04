"""Read-only projection of native side tasks for the scoped owner request."""
import sqlite3

from fastapi import HTTPException
from hermes_constants import get_hermes_home
from hermes_state_holders import read_only_db_uri
from hermes_state_sessions import _LINEAGE_CTE_SQL
from plugins.cloudseed_mobile.scope import profile_id
from tui_gateway.side_task_activity import registry


def session_activity(profile, stored_session_id):
    if profile != profile_id():
        raise HTTPException(403, 'Profile not found')
    if not stored_session_id or len(stored_session_id) > 512 or '\x00' in stored_session_id:
        raise ValueError('Invalid stored session id')
    home = get_hermes_home()
    parents = [stored_session_id]
    path = home / 'state.db'
    # Do not instantiate SessionDB: even initialization may migrate/create state.
    if path.is_file():
        db = sqlite3.connect(read_only_db_uri(path), uri=True, timeout=1)
        try:
            parents = [row[0] for row in db.execute(
                _LINEAGE_CTE_SQL + ' SELECT id FROM lineage',
                (stored_session_id, stored_session_id))]
        finally:
            db.close()
    return {'version': 1, 'profile': profile, 'stored_session_id': stored_session_id,
            'epoch': registry.epoch, 'complete': True,
            'tasks': registry.snapshot(home, parents)}
