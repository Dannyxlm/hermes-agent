"""Background warmer that lets the Files index converge without a Files open (round 9 U29, R24).

The deliverables index only refreshed after ``GET /deliverables`` (≤ 0.75 s of work per
open), so a ``scan_version`` bump or a grant change took hundreds of Files opens to reach
every session. One bounded worker, started and stopped with the dashboard, calls the same
checkpointed ``refresh`` while the index reports ``warm_pending``. It never chases ordinary
``state.db`` churn: a completed pass stays current until the next Files open, a grant
change or a version bump.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable, Optional

LOG = logging.getLogger(__name__)

IDLE = "idle"
WORKING = "working"
BACKOFF = "backoff"


class DeliverablesWarmer:
    """Single daemon thread; ``stop()`` cancels the in-flight refresh and joins."""

    def __init__(self, indexes: Callable[[], Iterable], *, initial_delay: float = 30.0,
                 interval: float = 10.0, idle_interval: float = 300.0, max_backoff: float = 300.0,
                 max_sessions: int = 100, wait: Optional[Callable[[float], bool]] = None):
        self._indexes = indexes
        self._initial_delay = initial_delay
        self._interval = interval
        self._idle_interval = idle_interval
        self._max_backoff = max_backoff
        self._max_sessions = max_sessions
        self._stop = threading.Event()
        # ``wait(seconds) -> stop requested``; injectable so the schedule is testable.
        self._wait = wait or self._stop.wait
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="cloudseed-deliverables-warmer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def run_once(self) -> str:
        """Advance every index that still needs warming by one bounded refresh.

        ``WORKING`` while any index is still pending, ``BACKOFF`` when one is busy
        (a Files-open refresh holds its lock) or failed, otherwise ``IDLE``.
        """
        state = IDLE
        for index in self._indexes():
            if self._stop.is_set():
                break
            if not index.snapshot().get("warm_pending"):
                continue
            if index.lock.locked():
                state = BACKOFF
                continue
            index.refresh(max_sessions=self._max_sessions, cancel=self._stop)
            after = index.snapshot()
            if after.get("refresh_status") == "source_unavailable":
                state = BACKOFF
            elif after.get("warm_pending") and state == IDLE:
                state = WORKING
        return state

    def _run(self) -> None:
        if self._wait(self._initial_delay):
            return
        backoff = self._interval
        while not self._stop.is_set():
            try:
                state = self.run_once()
            except Exception as exc:  # a failed scan resumes from its checkpoint next time
                LOG.debug("deliverables warmer pass failed: %s", type(exc).__name__)
                state = BACKOFF
            if state == BACKOFF:
                delay = backoff
                backoff = min(backoff * 2, self._max_backoff)
            else:
                backoff = self._interval
                delay = self._interval if state == WORKING else self._idle_interval
            if self._wait(delay):
                return
