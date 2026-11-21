"""Legacy import path for the SQLite implementation."""

from persistence.sqlite.authority._mutation_read import (
    set_read_in_transaction,
)

__all__ = [
    'set_read_in_transaction',
]
