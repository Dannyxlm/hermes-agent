"""The read lane of one WebSocket: read-only projections answered beside its serial control lane.

``handle_ws`` answers a socket's frames in arrival order, one at a time, so an inexpensive control
(an interrupt, a capability handshake, a queue edit) queues behind whatever the lane is running. A
phone foregrounding fires a dozen requests at once over one socket, several of them projections that
take seconds under load (the family graph, the profile roster, model discovery), and each one held up
everything that arrived after it. Here those projections run off the serial lane.

Which methods qualify is read from their handlers, not their names: every handler a socket can reach
for the method (the session authority's, and the legacy sidecar's, reached directly on a dashboard
socket or through the authority's fallback) only projects state that already exists. None of them
changes the connection (``client.capabilities`` sets this socket's frame policy), subscribes or
attaches (``session.resume``), leaves a cursor the next request spends (``session.events.since`` on
the authority keeps the replay position ``session.resume`` consumes), or writes a store.

Ordering stays the serial lane's. The dispatcher still takes frames in arrival order and hands a
read over only once every frame that arrived before it has been dispatched, so a read issued after a
write on the same socket sees that write, as it did on the serial lane. Only frames that arrive after
a read stop waiting for it: their requests never depended on its reply, which the client correlates
by id. A read naming a profile that is still starting waits in that profile's held lane, behind the
frames held there before it.

Each read has a cost class, from the server's own per-method latency (``route_metrics``): ``SLOW``
projections take seconds under load, ``FAST`` ones tens of milliseconds. The classes run under
separate per-socket caps, so a fast read never waits for a slot behind slow ones; no more than
``PER_METHOD_LIMIT`` reads of one method run at once, so a client polling one projection in a loop
cannot crowd out the others; ``ADMITTED_LIMIT`` running and waiting reads in all, past which the
reader parks as it does behind a busy serial lane.

On the legacy dispatcher a read runs to completion on ``_POOL``: never on the long-handler pool every
client's long controls share, nor on the loop's default executor connection setup and teardown wait
on, so its slot is held until its reply is written. On the session authority a read is a coroutine
whose blocking steps take the loop's default executor, which the owner's own commits (admissions,
session writes) use too: its caps are lower, leaving most of that executor to them.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
from functools import partial
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)

FAST, SLOW = "fast", "slow"
READ_METHODS = {
    # Session-bound projections of an attached session's live state.
    "session.context_breakdown": SLOW, "subagent.list": SLOW, "process.list": FAST,
    # Process- and store-wide rosters (Inbox, Agents, Projects, model picker, skill review, usage).
    "profiles.list": SLOW, "session.family": SLOW, "family.overview": SLOW, "model.options": SLOW,
    "skills.changes.list": SLOW, "usage.rollup": SLOW,
    "session.active_list": FAST, "subagent.roster": FAST, "projects.list": FAST, "spawn_tree.list": FAST,
    "mobile.capabilities": FAST,
}
# Running reads per socket, per class. The legacy dispatcher runs them on _POOL; the authority's
# blocking steps share the loop's default executor (min(32, cpus + 4) threads) with its commits.
# Kept near the phone's foreground burst (five to seven slow projections at once): in a model of that
# burst (20 bursts, real handle_ws, handler bodies held for their measured time) five slow slots beat
# the old dispatcher on every slow read, three left the later ones up to 1.4 s behind it, and a sixth
# sped up only the read it admitted. Past that, GIL-held projection work finishes no sooner and only
# stretches every other thread's wait for the GIL, the event loop's (heartbeats, streamed tokens)
# included: six slow plus six fast slots took the loop's ping p90 to 477 ms in the CPU-bound model.
LEGACY_LIMITS = {FAST: 4, SLOW: 5}
AUTHORITY_LIMITS = {FAST: 3, SLOW: 2}
PER_METHOD_LIMIT = 2
ADMITTED_LIMIT = 64
# Every socket's legacy reads: two sockets' full lanes (the phone and a Desktop window) at once.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2 * sum(LEGACY_LIMITS.values()),
                                              thread_name_prefix="ws-read")


def is_read(req: Any) -> bool:
    return isinstance(req, dict) and req.get("method") in READ_METHODS


async def run_blocking(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """``asyncio.to_thread`` on the read pool: the caller's context (transport, profile scope, the
    route-metrics observation) carries into the worker."""
    call = partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_POOL, call)


class ReadLane:
    """One socket's reads: admitted by its reader, started by its dispatcher, ended at teardown."""

    def __init__(self, *, on_authority: bool) -> None:
        limits = AUTHORITY_LIMITS if on_authority else LEGACY_LIMITS
        self._admitted = asyncio.Semaphore(ADMITTED_LIMIT)
        self._running = {cost: asyncio.Semaphore(limit) for cost, limit in limits.items()}
        self._per_method: dict[str, asyncio.Semaphore] = {}
        self._tasks: set[asyncio.Task] = set()
        self._started: set[asyncio.Task] = set()

    async def admit(self) -> None:
        """The reader takes a slot before it queues a read; past ``ADMITTED_LIMIT`` it parks."""
        await self._admitted.acquire()

    def withdraw(self) -> None:
        """An admitted read went somewhere else (a profile-start hold lane, which bounds it itself)."""
        self._admitted.release()

    def start(self, method: str, work: Callable[[], Awaitable[None]]) -> None:
        """Run *work* (one read's dispatch and reply) once its slots are free. Called by the
        dispatcher in arrival order, after every earlier frame on the serial lane was dispatched."""
        task = asyncio.create_task(self._run(method, work))
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    async def _run(self, method: str, work: Callable[[], Awaitable[None]]) -> None:
        per_method = self._per_method.setdefault(method, asyncio.Semaphore(PER_METHOD_LIMIT))
        async with per_method, self._running[READ_METHODS[method]]:
            self._started.add(asyncio.current_task())  # type: ignore[arg-type]  # always inside a task
            await work()

    def _finished(self, task: asyncio.Task) -> None:
        # Here, not in the task: a task cancelled before its first step never runs its body.
        self._admitted.release()
        self._tasks.discard(task)
        self._started.discard(task)
        if not task.cancelled() and (failure := task.exception()) is not None:
            _log.error("ws read lane task failed", exc_info=failure)

    async def end(self, *, shutting_down: bool) -> None:
        """Reads that never started are dropped (their client is gone); started ones finish, unless
        the connection itself was cancelled (server shutdown)."""
        for task in self._tasks:
            if shutting_down or task not in self._started:
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
