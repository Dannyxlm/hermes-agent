"""Full snapshots for legacy stores; indexed projection deltas for journaled stores."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from hermes_state_common import _id_chunks, _placeholders
from hermes_state_session_changes import SESSION_SIGNATURE_FIELDS, changed_ids, journal_available, journal_window


@dataclass
class Observation:
    schema: int
    through: int | None
    fields: tuple
    fingerprints: dict
    digest: int

    @property
    def signature(self):
        # Commutative: adding/removing just one row never rehashes all history.
        return self.fields, self.digest, len(self.fingerprints)


@contextmanager
def projection_snapshot(conn, previous: Observation | None):
    """Keep watermark, rows AND delegate classification in one SQLite snapshot.

    A pre-transaction data_version can cause one redundant pass on a racing
    commit, but never advance the watermark beyond the rows actually observed.
    """
    conn.execute("BEGIN")
    try:
        yield _read_projection(conn, previous)
    finally:
        conn.rollback()


def _read_projection(conn, previous: Observation | None):
    """DDL, retention gaps and older stores always require a complete read."""
    schema = conn.execute("PRAGMA schema_version").fetchone()[0]
    available = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    fields = tuple(field for field in SESSION_SIGNATURE_FIELDS if field in available)
    if not fields:
        return None, {}, set(), available
    journaled = "id" in fields and journal_available(conn)
    low, high = journal_window(conn) if journaled else (0, 0)
    ids = None
    if (journaled and previous is not None and previous.through is not None
            and previous.schema == schema and previous.fields == fields
            and low - 1 <= previous.through <= high):
        ids = changed_ids(conn, previous.through)
        if not ids:
            return previous, {}, set(), available
    observed = _read_rows(conn, fields, ids)
    removed = None if ids is None else ids - observed.keys()
    if ids is not None and previous is not None:
        # Stage modifications: a failed publication must not leave the retained
        # watermark/fingerprint ahead of its cursor observation.
        fingerprints = previous.fingerprints.copy()
        digest = previous.digest
        for sid in ids:
            digest ^= fingerprints.pop(sid, 0)
    else:
        fingerprints, digest = {}, 0
    for sid, (fingerprint, _values) in observed.items():
        fingerprints[sid] = fingerprint
        digest ^= fingerprint
    return Observation(schema, high if journaled else None, fields, fingerprints, digest), observed, removed, available


def _read_rows(conn, fields, ids):
    columns = ", ".join(fields)
    rows = []
    if ids is None:
        order = " ORDER BY id" if "id" in fields else ""
        rows = conn.execute(f"SELECT {columns} FROM sessions{order}").fetchall()
    else:
        for chunk in _id_chunks(sorted(ids)):
            rows.extend(conn.execute(f"SELECT {columns} FROM sessions WHERE id IN ({_placeholders(chunk)})", chunk))
    id_at = fields.index("id") if "id" in fields else None
    return {(row[id_at] if id_at is not None else i): (hash(tuple(row)), tuple(row)) for i, row in enumerate(rows)}
