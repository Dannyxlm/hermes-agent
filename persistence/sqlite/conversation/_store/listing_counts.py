"""Paired listing totals without rescanning the same visibility scope."""
from .sessions import _session_filter_where, _where_sql, _own_profile_name
from .local import exclude_superseded_segments


def listing_counts(db, *, include_archived=False, archived_only=False, exclude_threads=False, **scope):
    where, params = _session_filter_where(
        include_archived=True, archived_only=False,
        thread_profile=(_own_profile_name(db) or "default") if exclude_threads else None, **scope)
    if scope.get('exclude_children'):
        exclude_superseded_segments(db, where, params)
    row = db._read_one(
        'SELECT COUNT(*), COUNT(CASE WHEN s.archived = 0 THEN 1 END), '
        'COUNT(CASE WHEN s.archived = 1 THEN 1 END) '
        f'FROM sessions s{_where_sql(where, " ")}', params)
    total, visible, archived = row
    return (archived if archived_only else total if include_archived else visible), archived
