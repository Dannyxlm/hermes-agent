"""Content-free, read-only release evidence from the canonical runtime ledger.

Callers supply a profile database and either an explicit session-ID allowlist or
None for an atomic whole-profile snapshot. None includes the lead and all ledger
targets, regardless of age. Each profile has its own SQLite read linearization;
this is not an all-time, cross-profile atomic, or all-OS-work guarantee.
No execution/admission/lease-holder linkage is invented from matching targets.
This is an operator reader, not an authorization boundary for untrusted clients.

Snapshot captures all unresolved admissions/workers without an age cutoff. Save
its EvidenceKeys for lookup after settlement/restart: lookup keeps terminal rows
and tombstones, rather than re-running an active-only query. An unclaimed
admission's null generation has no execution fence yet; once bound, its saved
generation must match exactly. Adoption may change its owner epoch: keep the
current row visible but mark that drift incomplete, never proof of continuity.

Only whitelisted metadata columns cross SQLite's result boundary. Tombstones
share a JSON column with private receipts, so SQL extracts named metadata paths
at an exact tombstone key; Python never loads the containing document. Retired
admissions discard lineage upstream: report that loss, never reconstruct it.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Literal

from hermes_cli.sqlite_safe_read import connect_tracked
from hermes_state_holders import read_only_db_uri


_ADMISSION_COLUMNS = ('admission_id', 'target_session_id', 'lineage_json', 'intent',
                      'status', 'outcome', 'owner_epoch', 'generation')
_WORKER_COLUMNS = ('execution_id', 'session_id', 'kind', 'status', 'owner_epoch', 'generation')
_TERMINAL_OUTCOMES = frozenset({'completed', 'interrupted', 'rejected', 'failed', 'cancelled', 'steered'})
_TOMBSTONE_PREFIX = {'admission': 'gateway.terminal_admission.v1.',
                     'execution': 'gateway.terminal_worker.v1.'}


@dataclass(frozen=True)
class ReleaseScope:
    """Exact session allowlist; exclusions win (e.g. the release's lead session).

    The caller owns scope discovery and must enumerate all relevant logical and
    physical targets. An empty effective scope is permitted only through explicit
    exclusions; an omitted/empty allowlist is never interpreted as 'all profiles'.
    """

    session_ids: tuple[str, ...]
    excluded_session_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.session_ids, (tuple, list))
                or not isinstance(self.excluded_session_ids, (tuple, list))
                or not self.session_ids or any(not isinstance(sid, str) or not sid
                    for sid in (*self.session_ids, *self.excluded_session_ids))):
            raise ValueError('explicit nonempty session IDs required')
        object.__setattr__(self, 'session_ids', tuple(dict.fromkeys(self.session_ids)))
        object.__setattr__(self, 'excluded_session_ids', tuple(dict.fromkeys(self.excluded_session_ids)))

    @property
    def included_session_ids(self) -> tuple[str, ...]:
        return tuple(sid for sid in self.session_ids if sid not in self.excluded_session_ids)


@dataclass(frozen=True)
class EvidenceKey:
    kind: Literal['admission', 'execution']
    id: str
    target_session_id: str
    owner_epoch: int | None
    generation: int | None

    def __post_init__(self):
        if (self.kind not in _TOMBSTONE_PREFIX or not isinstance(self.id, str) or not self.id
                or not isinstance(self.target_session_id, str) or not self.target_session_id):
            raise ValueError('invalid evidence identity')
        for value in (self.owner_epoch, self.generation):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('invalid evidence fence')
        if self.kind == 'execution' and (self.owner_epoch is None or self.generation is None):
            raise ValueError('execution evidence requires epoch and generation')


@dataclass(frozen=True)
class ReleaseEvidence:
    key: EvidenceKey
    status: str
    lineage: tuple[str, ...] | None
    intent: str | None = None
    outcome: str | None = None
    worker_kind: str | None = None
    storage: Literal['live', 'tombstone'] = 'live'

    @property
    def terminal(self) -> bool:
        return self.status == 'terminal' and (
            self.key.kind == 'execution' or self.outcome in _TERMINAL_OUTCOMES)

    @property
    def completed_turn(self) -> bool:
        """A settled turn, never a queued/inflight or delivered steering input."""
        return self.key.kind == 'admission' and self.terminal and self.outcome == 'completed'


@dataclass(frozen=True)
class ReleaseEvidenceBatch:
    scope: ReleaseScope | None
    authority_epoch: int | None
    authority_instance_id: str | None
    rows: tuple[ReleaseEvidence, ...]
    missing_keys: tuple[EvidenceKey, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Coverage/identity completeness, NOT a verdict that work completed."""
        return not self.issues and not self.missing_keys


@contextmanager
def _reader(db_path: str | Path):
    # mode=ro refuses a missing file; immutable=1 would incorrectly ignore a live WAL.
    conn = connect_tracked(read_only_db_uri(db_path), uri=True, tracking_path=db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _has_columns(conn, table, columns, issues):
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if found and set(columns) <= {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}:
        return True
    issues.append('schema_incomplete')
    return False


def _authority(conn, issues):
    if _has_columns(conn, 'runtime_epoch', ('singleton', 'epoch', 'instance_id'), issues):
        row = conn.execute('SELECT epoch,instance_id FROM runtime_epoch WHERE singleton=1').fetchone()
        if (row and type(row['epoch']) is int and row['epoch'] > 0
                and isinstance(row['instance_id'], str) and row['instance_id']):
            return row['epoch'], row['instance_id']
    issues.append('authority_unavailable')
    return None, None


def _read_admissions(conn, scope, issues, *, ids=None, active_only=False):
    if not _has_columns(conn, 'session_admissions', _ADMISSION_COLUMNS, issues):
        return []
    sql = 'SELECT ' + ','.join(_ADMISSION_COLUMNS) + ' FROM session_admissions WHERE 1'
    params = []
    if scope is not None:
        sql += ' AND target_session_id IN (SELECT value FROM json_each(?))'
        params.append(json.dumps(scope.included_session_ids))
    if ids is not None:
        sql += ' AND admission_id IN (SELECT value FROM json_each(?))'
        params.append(json.dumps(ids))
    if active_only:
        # terminal/steering is a transfer still owned by the receiving turn, not completion.
        sql += " AND (status!='terminal' OR outcome IS NULL OR outcome NOT IN "
        sql += "('completed','interrupted','rejected','failed','cancelled','steered'))"
    return conn.execute(sql + ' ORDER BY admission_id', params).fetchall()


def _read_workers(conn, scope, issues, *, ids=None, active_only=False):
    if not _has_columns(conn, 'worker_executions', _WORKER_COLUMNS, issues):
        return []
    sql = 'SELECT ' + ','.join(_WORKER_COLUMNS) + ' FROM worker_executions WHERE 1'
    params = []
    if scope is not None:
        sql += ' AND session_id IN (SELECT value FROM json_each(?))'
        params.append(json.dumps(scope.included_session_ids))
    if ids is not None:
        sql += ' AND execution_id IN (SELECT value FROM json_each(?))'
        params.append(json.dumps(ids))
    if active_only:
        sql += " AND status!='terminal'"
    return conn.execute(sql + ' ORDER BY execution_id', params).fetchall()


def _lineage(raw, issues):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, list) and value and all(isinstance(sid, str) and sid for sid in value):
        return tuple(value)
    issues.append('lineage_unavailable')
    return None


def _project(raw, kind: Literal['admission', 'execution'], issues,
             *, storage: Literal['live', 'tombstone'] = 'live'):
    if kind == 'admission':
        lineage = _lineage(raw['lineage_json'], issues)
        key = EvidenceKey(kind, raw['admission_id'], raw['target_session_id'], raw['owner_epoch'], raw['generation'])
        if raw['status'] not in ('queued', 'started', 'unknown', 'terminal'):
            issues.append('status_unavailable')
        if raw['status'] == 'terminal' and raw['outcome'] not in _TERMINAL_OUTCOMES | {'steering'}:
            issues.append('outcome_unavailable')
        if (key.owner_epoch is None or (key.generation is None
                and (raw['status'] in ('started', 'unknown') or raw['outcome'] == 'steering'))):
            issues.append('identity_incomplete')
        return ReleaseEvidence(key, raw['status'], lineage, intent=raw['intent'], outcome=raw['outcome'], storage=storage)
    key = EvidenceKey(kind, raw['execution_id'], raw['session_id'], raw['owner_epoch'], raw['generation'])
    if raw['status'] not in ('registered', 'running', 'unknown', 'terminal'):
        issues.append('status_unavailable')
    # worker_executions stores a target, not a lineage. Do not invent a link to an admission.
    return ReleaseEvidence(key, raw['status'], None, worker_kind=raw['kind'], storage=storage)


def _whole_profile_lease_coverage(conn, rows, issues, read_at):
    """Conservative SQLite lease coverage; never infer OS holder liveness.

    Walk exactly the writer's compression-parent edges (including subagent
    continuations but not parent-bound branch/delegate/reset forks or tools).
    Only root IDs cross the result boundary; model_config and holders do not.
    An unexpired lease without unresolved ledger work cannot be certified. Idle
    historical sessions are not admissions and do not become synthetic turns.
    """
    if not _has_columns(conn, 'session_turn_leases', ('conversation_id', 'expires_at'), issues):
        return
    if not _has_columns(conn, 'sessions',
                        ('id', 'parent_session_id', 'source', 'model_config', 'end_reason'), issues):
        return
    edge = "parent.end_reason='compression' AND COALESCE(child.source,'')!='tool'"
    for marker in ('_branched_from', '_delegate_from', '_reset_from'):
        edge += (" AND COALESCE(CASE WHEN json_valid(child.model_config) THEN "
                 f"json_extract(child.model_config,'$.{marker}') END,'')!=parent.id")
    # A queued admission or delivered steering input cannot account for an
    # executing legacy turn just because it happens to share the same target.
    targets = tuple(dict.fromkeys(row.key.target_session_id for row in rows
        if row.key.kind == 'execution' or row.status in ('started', 'unknown')))
    untracked = conn.execute(f"""WITH RECURSIVE lineage(id) AS (
        SELECT value FROM json_each(?)
        UNION
        SELECT parent.id FROM lineage r JOIN sessions child ON child.id=r.id
        JOIN sessions parent ON parent.id=child.parent_session_id WHERE {edge}
    ), roots(id) AS (
        SELECT r.id FROM lineage r WHERE NOT EXISTS (
            SELECT 1 FROM sessions child JOIN sessions parent ON parent.id=child.parent_session_id
            WHERE child.id=r.id AND {edge})
    ) SELECT 1 FROM session_turn_leases l WHERE l.expires_at>?
      AND NOT EXISTS (SELECT 1 FROM roots WHERE roots.id=l.conversation_id) LIMIT 1""",
        (json.dumps(targets), read_at)).fetchone()
    if untracked:
        issues.append('untracked_active_lease')
    elif conn.execute('SELECT 1 FROM session_turn_leases WHERE expires_at>? LIMIT 1',
                      (read_at,)).fetchone():
        # The schema has no holder -> admission/execution binding. A matching
        # root tells us which ledger to report, not who owns the active lease.
        issues.append('lease_identity_unavailable')


def _snapshot_coverage(conn, scope, issues, rows, read_at):
    if not _has_columns(conn, 'sessions', ('id',), issues):
        return
    if 'schema_incomplete' in issues:
        return
    if scope is None:
        _whole_profile_lease_coverage(conn, rows, issues, read_at)
        return
    for sid in scope.included_session_ids:
        if not conn.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
            issues.append('scope_missing')
        elif not conn.execute('SELECT 1 FROM session_admissions WHERE target_session_id=? UNION ALL '
                              'SELECT 1 FROM worker_executions WHERE session_id=? LIMIT 1', (sid, sid)).fetchone():
            issues.append('untracked_session')


def snapshot_release_evidence(db_path: str | Path, *, scope: ReleaseScope | None = None) -> ReleaseEvidenceBatch:
    """One consistent SQLite snapshot of active work, optionally whole-profile.

    No payloads, transcript bodies, SessionDB constructors, migrations
    or writes are used. Explicit scopes retain their legacy untracked-session
    uncertainty. Whole-profile coverage is bounded to the ledger and turn leases
    at the first read in this transaction, not all OS work or future arrivals.
    """
    issues = []
    epoch, instance = None, None
    rows = []
    try:
        with _reader(db_path) as conn:
            # BEGIN is deferred: this database read pins the WAL snapshot and
            # its lease-expiry clock BEFORE any interleaved ledger reads.
            read_at = conn.execute("SELECT (julianday('now')-2440587.5)*86400 "
                                   "FROM sqlite_master LIMIT 1").fetchone()
            epoch, instance = _authority(conn, issues)
            admissions = _read_admissions(conn, scope, issues, active_only=True)
            workers = _read_workers(conn, scope, issues, active_only=True)
            rows = [_project(row, 'admission', issues) for row in admissions]
            rows.extend(_project(row, 'execution', issues) for row in workers)
            _snapshot_coverage(conn, scope, issues, rows, read_at[0] if read_at else 0)
    except (sqlite3.Error, OSError):
        issues.append('database_unavailable')
        rows = []
    except (TypeError, ValueError):
        issues.append('metadata_unavailable')
        rows = []
    return ReleaseEvidenceBatch(scope, epoch, instance, tuple(rows), issues=tuple(dict.fromkeys(issues)))


def _tombstone(conn, key, issues):
    if not _has_columns(conn, 'state_meta', ('key', 'value'), issues):
        return None
    columns = _ADMISSION_COLUMNS if key.kind == 'admission' else _WORKER_COLUMNS
    # Never SELECT value: even terminal worker JSON can contain private read receipts.
    fields = ','.join(f"json_extract(value,'$.{column}') AS {column}" for column in columns)
    return conn.execute(f'SELECT {fields} FROM state_meta WHERE key=? AND json_valid(value)',
                        (_TOMBSTONE_PREFIX[key.kind] + key.id,)).fetchone()


def _matches(expected, actual):
    if (expected.kind, expected.id, expected.target_session_id) != (
            actual.kind, actual.id, actual.target_session_id):
        return False
    # Queued work has an admission epoch, but no execution identity until claimed.
    if expected.kind == 'admission' and expected.generation is None:
        return True
    return expected.generation == actual.generation


def _lookup_rows(conn, scope, keys, issues):
    admissions = _read_admissions(conn, scope, issues, ids=[key.id for key in keys if key.kind == 'admission'])
    workers = _read_workers(conn, scope, issues, ids=[key.id for key in keys if key.kind == 'execution'])
    live = {('admission', row['admission_id']): row for row in admissions}
    live.update({('execution', row['execution_id']): row for row in workers})
    rows, missing = [], []
    for key in keys:
        if scope is not None and key.target_session_id not in scope.included_session_ids:
            issues.append('outside_scope')
            missing.append(key)
            continue
        raw = live.get((key.kind, key.id))
        storage: Literal['live', 'tombstone'] = 'live'
        if raw is None:
            raw = _tombstone(conn, key, issues)
            storage = 'tombstone'
        if raw is None:
            issues.append('missing_identity')
            missing.append(key)
            continue
        row_issues = []
        row = _project(raw, key.kind, row_issues, storage=storage)
        if not _matches(key, row.key):
            issues.append('identity_mismatch')
            missing.append(key)
            continue
        if key.generation is not None and key.owner_epoch != row.key.owner_epoch:
            row_issues.append('owner_epoch_changed')
        issues.extend(row_issues)
        rows.append(row)
    return rows, missing


def lookup_release_evidence(db_path: str | Path, *, scope: ReleaseScope | None = None,
                            keys: tuple[EvidenceKey, ...]) -> ReleaseEvidenceBatch:
    """Read saved exact IDs, including terminal rows/tombstones, in one transaction.

    No status filter is applied. Missing, excluded/cross-scope, replaced identities
    and unavailable legacy evidence remain incomplete. The current authority epoch
    is reported separately and never substituted into a saved execution identity.
    Epoch drift on a bound identity keeps its current status visible but marks the
    batch incomplete: this reader cannot certify the intervening adoption proof.
    Tombstone retention is upstream-owned; expired/missing rows cannot prove completion.
    None removes the session allowlist, NOT the exact saved-key bounds: only the
    requested IDs are selected, and each target and nullable fence is preserved.
    """
    issues = []
    epoch, instance = None, None
    rows, missing = [], list(keys)
    try:
        with _reader(db_path) as conn:
            epoch, instance = _authority(conn, issues)
            rows, missing = _lookup_rows(conn, scope, keys, issues)
    except (sqlite3.Error, OSError):
        issues.append('database_unavailable')
        rows, missing = [], list(keys)
    except (TypeError, ValueError):
        issues.append('metadata_unavailable')
        rows, missing = [], list(keys)
    return ReleaseEvidenceBatch(scope, epoch, instance, tuple(rows), tuple(missing), tuple(dict.fromkeys(issues)))
