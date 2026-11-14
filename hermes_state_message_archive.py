"""Lease-fenced soft retirement of transcript segments."""

from __future__ import annotations

from typing import Optional


class SessionMessageArchiveMixin:
    """Transcript retirement using the SessionDB writer and turn-lease fence."""

    def archive_session_messages(
        self,
        session_ids: list[str],
        turn_lease_holder: Optional[str] = None,
    ) -> int:
        """Soft-retire active messages for several compression segments."""
        unique_ids = list(dict.fromkeys(
            sid for sid in session_ids if isinstance(sid, str) and sid
        ))
        if not unique_ids:
            return 0

        def _do(conn):
            if turn_lease_holder:
                self._assert_session_turn_lease_holder_on_conn(
                    conn,
                    unique_ids,
                    turn_lease_holder,
                )
            placeholders = ",".join("?" * len(unique_ids))
            cursor = conn.execute(
                f"UPDATE messages SET active = 0, compacted = 0 "
                f"WHERE active = 1 AND session_id IN ({placeholders})",
                unique_ids,
            )
            conn.execute(
                f"UPDATE sessions SET message_count = 0, tool_call_count = 0 "
                f"WHERE id IN ({placeholders})",
                unique_ids,
            )
            return cursor.rowcount

        return int(self._execute_write(_do) or 0)
