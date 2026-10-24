"""Observation-only session activity writers; heartbeat cadence belongs to the caller."""
import sqlite3
import time
from typing import Optional
from agent.session_activity import ActivityProvenance, bound_activity_description, normalize_activity_provenance


class SessionActivityMixin:
    def touch_session_activity(
        self, session_id: str, ts: Optional[float] = None, *, description: Optional[str] = None,
        provenance: Optional[ActivityProvenance] = None, step_kind: Optional[str] = None, step_tool: Optional[str] = None,
    ) -> None:
        """Stamp durable mid-turn activity (observation-only; rate-limited by the caller) so surfaces see
        activity before any message row lands. Never moves ``last_activity_at`` backwards.

        Called (rate-limited) from ``AIAgent._touch_activity`` so gateway/CLI surfaces and stall consumers
        observe API/tool/compaction activity even when no new message row has been written yet (#72016 /
        #72039).
        """
        if not session_id:
            return
        from agent.session_activity import validate_step
        step_kind, step_tool = validate_step(step_kind, step_tool)
        when = float(ts if ts is not None else time.time())
        self._write_sql(
            "UPDATE sessions SET last_activity_at = ?, "
            "last_activity_description = ?, last_activity_provenance = ?, last_activity_kind = ?, last_activity_tool = ? "
            "WHERE id = ? AND (last_activity_at IS NULL OR last_activity_at < ?)",
            (
                when, bound_activity_description(description),
                normalize_activity_provenance(provenance).value, step_kind, step_tool, session_id, when,
            ),
            patience_s=self._ACTIVITY_WRITE_PATIENCE_S,
        )

    def clear_session_activity_labels(self, session_id: str) -> None:
        """Clear activity labels after a turn (``last_activity_at`` is kept so idle / watchdog clocks stay
        continuous). A no-op clear skips the write transaction.

        Description and provenance are observation labels for *what was happening at* that timestamp during
        an active turn; once the turn is idle they must not keep advertising "compressing" / "executing
        tool" (#72039).
        Response-critical-path contract (#76354 review S1): runs in the turn's ``finally``; a no-op clear
        (labels already empty) skips the write transaction entirely, and a real clear uses the same short
        sub-second busy budget as :meth:`touch_session_activity` instead of the full routine write patience.
        """
        if not session_id:
            return
        try:
            row = self._read_one(
                "SELECT last_activity_description, last_activity_provenance, last_activity_kind, last_activity_tool "
                "FROM sessions WHERE id = ?",
                (session_id,),
            )
        except sqlite3.Error:
            row = None
        if (row is not None and not row[0] and (not row[1] or row[1] == ActivityProvenance.UNKNOWN.value)
                and row[2] in (None, "idle") and row[3] is None):
            return
        self._write_sql(
            "UPDATE sessions SET last_activity_description = ?, last_activity_provenance = ?, "
            "last_activity_kind = 'idle', last_activity_tool = NULL WHERE id = ?",
            ("", ActivityProvenance.UNKNOWN.value, session_id), patience_s=self._ACTIVITY_WRITE_PATIENCE_S,
        )
