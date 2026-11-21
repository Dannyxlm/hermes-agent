"""Legacy import path for the SQLite implementation."""

from persistence.sqlite.conversation._store.lineage_paging import (
    indexed_lineage_page, lineage_display_page,
)

__all__ = [
    'indexed_lineage_page', 'lineage_display_page',
]
