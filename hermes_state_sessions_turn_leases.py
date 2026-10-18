"""Turn-lease fence for bulk session deletes (CloudSeed overlay ``hermex-shared-state``).

Moved out of ``SessionSessionsMixin.delete_sessions`` unchanged, so the overlay carries fewer lines inside
the upstream-owned ``hermes_state_sessions`` module.
"""

from __future__ import annotations

import time
from typing import Any, Optional


def fence_delete_turn_leases(db: Any, conn, target_ids: list[str], turn_lease_holder: Optional[str]) -> None:
    """Inside the delete transaction: reap expired or dead-holder turn leases on *target_ids*, and refuse
    the delete when a live lease is held by anyone other than *turn_lease_holder*."""
    from hermes_state_errors import SessionTurnLeaseLostError
    from hermes_state import _compression_lock_holder_process_is_dead
    now, checked_keys = time.time(), set()
    for target_id in target_ids:
        conversation_id = db._session_turn_lease_key_on_conn(conn, target_id)
        if conversation_id in checked_keys:
            continue
        checked_keys.add(conversation_id)
        lease = conn.execute("SELECT holder, expires_at FROM session_turn_leases WHERE conversation_id = ?",
                             (conversation_id,)).fetchone()
        if lease is None:
            continue
        holder = lease["holder"]
        if float(lease["expires_at"]) <= now or _compression_lock_holder_process_is_dead(holder):
            conn.execute("DELETE FROM session_turn_leases WHERE conversation_id = ? AND holder = ?",
                         (conversation_id, holder))
        elif not turn_lease_holder or holder != turn_lease_holder:
            raise SessionTurnLeaseLostError(f"Session {target_id!r} has an active delegated turn")
