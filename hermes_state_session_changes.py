"""Legacy import path for the SQLite implementation."""

from persistence.sqlite._session_changes import (
    JOURNAL_SCHEMA_SQL, RETAIN_CHANGES, SESSION_SIGNATURE_FIELDS,
    TABLE, TRIGGERS, _TRIGGER_DEFINITIONS,
    changed_ids, ensure_session_change_journal, journal_available,
    journal_window,
)

__all__ = [
    'JOURNAL_SCHEMA_SQL', 'RETAIN_CHANGES', 'SESSION_SIGNATURE_FIELDS',
    'TABLE', 'TRIGGERS', '_TRIGGER_DEFINITIONS',
    'changed_ids', 'ensure_session_change_journal', 'journal_available',
    'journal_window',
]
