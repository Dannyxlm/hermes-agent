"""Paired listing totals without rescanning the same visibility scope."""
from hermes_state_sessions import _session_filter_where
from hermes_state_sessions_metadata import _where_sql
from hermes_state_local import exclude_superseded_segments


def listing_counts(db, *, include_archived=False, archived_only=False, **scope):
    where, params = _session_filter_where(
        include_archived=True, archived_only=False, **scope)
    if scope.get('exclude_children'):
        exclude_superseded_segments(db, where, params)
    row = db._read_one(
        'SELECT COUNT(*), COUNT(CASE WHEN s.archived = 0 THEN 1 END), '
        'COUNT(CASE WHEN s.archived = 1 THEN 1 END) '
        f'FROM sessions s{_where_sql(where, " ")}', params)
    total, visible, archived = row
    return (archived if archived_only else total if include_archived else visible), archived
