"""Read-only projection of native side tasks for the scoped owner request."""
import sqlite3
import logging
import math

from fastapi import HTTPException
from hermes_constants import get_hermes_home
from hermes_state_holders import read_only_db_uri
from hermes_state_sessions import _LINEAGE_CTE_SQL
from plugins.cloudseed_mobile.scope import profile_id
from tui_gateway.side_task_activity import registry

logger = logging.getLogger(__name__)
MAX_ACTIVITY_BATCH = 50


def _valid_id(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512 and '\x00' not in value


def _lineages(home, ids):
    parents = {sid: [sid] for sid in ids}
    path = home / 'state.db'
    if not path.is_file():
        return parents
    # A batch opens one read-only connection, never SessionDB (which can migrate).
    db = sqlite3.connect(read_only_db_uri(path), uri=True, timeout=1)
    try:
        sql = """
            WITH RECURSIVE roots(root) AS (VALUES %s),
            ancestors(root,id) AS (
                SELECT root,root FROM roots
                UNION
                SELECT a.root,parent.id FROM ancestors a
                JOIN sessions child ON child.id=a.id
                JOIN sessions parent ON parent.id=child.parent_session_id
                WHERE parent.end_reason='compression'
            ),
            descendants(root,id) AS (
                SELECT root,root FROM roots
                UNION
                SELECT d.root,child.id FROM descendants d
                JOIN sessions parent ON parent.id=d.id
                JOIN sessions child ON child.parent_session_id=parent.id
                WHERE parent.end_reason='compression'
            )
            SELECT root,id FROM ancestors UNION SELECT root,id FROM descendants
        """ % ','.join('(?)' for _ in ids)
        parents = {sid: [] for sid in ids}
        for root, member in db.execute(sql, ids):
            parents[root].append(member)
    finally:
        db.close()
    return parents


def _agent_snapshot():
    from tools.activity_provenance import incomplete_subagent_scopes
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
    # Read admission fences first: async admission precedes call-fence removal.
    incomplete = incomplete_subagent_scopes()
    fields = ('subagent_id', 'status', 'started_at', 'owner_profile_home', 'owner_conversation_id')
    with _active_subagents_lock:
        return [{key: row.get(key) for key in fields} for row in _active_subagents.values()] + incomplete


def _process_snapshot():
    from tools.process_registry_activity import process_activity_snapshot
    return process_activity_snapshot()


def _read_workers(read):
    try:
        return read()
    except Exception:
        # This domain is unavailable; other domains still carry positive evidence.
        logger.warning('Worker metadata snapshot unavailable', exc_info=True)
        return None


def _scoped_workers(rows, home, parents, identity):
    if not isinstance(rows, list):
        return None, False
    result, complete = [], True
    for row in rows:
        if not isinstance(row, dict):
            complete = False
            continue
        owner = row.get('owner_profile_home')
        if not isinstance(owner, str) or not owner:
            complete = False  # Old records cannot prove which profile owns them.
            continue
        if owner != home:
            continue
        conversation = row.get('owner_conversation_id')
        if not _valid_id(conversation):
            complete = False
            continue
        if conversation not in parents:
            continue
        if row.get('_snapshot_incomplete') is True:
            complete = False
            continue
        status = row.get('status')
        if status in ('completed', 'failed', 'timeout', 'interrupted', 'exited'):
            continue
        if not _valid_id(row.get(identity)) or status not in ('running', 'queued'):
            complete = False
            continue
        item = {identity: row[identity], 'status': status}
        started = row.get('started_at')
        if isinstance(started, (int, float)) and not isinstance(started, bool) and math.isfinite(started):
            item['started_at'] = started
        result.append(item)
    return result, complete


def _worker_batches(rows, home, reverse, roots):
    if not isinstance(rows, list):
        return {root: None for root in roots}
    batches = {root: [] for root in roots}
    for row in rows:
        if (not isinstance(row, dict)
                or not isinstance(row.get('owner_profile_home'), str)
                or not row['owner_profile_home']):
            targets = roots  # Unknown ownership affects every root's completeness.
        elif row['owner_profile_home'] != home:
            continue
        elif not _valid_id(row.get('owner_conversation_id')):
            targets = roots
        else:
            targets = reverse.get(row['owner_conversation_id'], ())
        for root in targets:
            batches[root].append(row)
    return batches


def session_activity_batch(profile, stored_session_ids):
    if profile != profile_id():
        raise HTTPException(403, 'Profile not found')
    if (not 1 <= len(stored_session_ids) <= MAX_ACTIVITY_BATCH
            or any(not _valid_id(sid) for sid in stored_session_ids)
            or len(set(stored_session_ids)) != len(stored_session_ids)):
        raise ValueError('Expected 1 to 50 unique stored session ids')
    home = get_hermes_home()
    lineages = _lineages(home, stored_session_ids)
    reverse = {}
    for root, members in lineages.items():
        for member in members:
            reverse.setdefault(member, []).append(root)
    owner_home = str(home.resolve())
    agents = _worker_batches(_read_workers(_agent_snapshot), owner_home, reverse, stored_session_ids)
    processes = _worker_batches(_read_workers(_process_snapshot), owner_home, reverse, stored_session_ids)
    sessions = []
    for sid in stored_session_ids:
        parents = set(lineages[sid])
        subagents, agents_complete = _scoped_workers(agents[sid], owner_home, parents, 'subagent_id')
        jobs, processes_complete = _scoped_workers(processes[sid], owner_home, parents, 'session_id')
        sessions.append({'version': 1, 'profile': profile, 'stored_session_id': sid,
                         'epoch': registry.epoch, 'complete': True, 'tasks': registry.snapshot(home, parents),
                         'subagents': subagents, 'subagents_complete': agents_complete,
                         'processes': jobs, 'processes_complete': processes_complete})
    return {'version': 1, 'profile': profile, 'epoch': registry.epoch, 'complete': True, 'sessions': sessions}


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
