"""The empty durable-queue surface shared by minimal resume/branch stores."""

import sqlite3


class EmptyQueueReceipts:
    """A single-segment fixture with no queued inputs, using the real receipt SQL."""

    def get_compression_lineage(self, session_id):
        return [session_id]

    def _execute_write(self, operation):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT)")
            return operation(conn)
        finally:
            conn.close()
