"""Quiet TUI-only post-turn compaction and its input-admission handshake.

The core automatic commit operates on isolated Python bookkeeping. An arriving
turn can detach pre-commit work immediately; only a commit winner is joined until
its DB result, agent delta and host history/version have all been published.
"""
from __future__ import annotations

import copy
import threading

from .method_ctx import bind_module


# Retain the slot until even an abandoned, uncooperative owner unwinds. Required
# in-turn compression shares the four-slot core pool and must not be starved.
_BETWEEN_TURNS_SLOT = threading.BoundedSemaphore(1)


class _BetweenTurnsCompaction:
    def __init__(self, agent, session, version):
        from agent.between_turns_compaction import SessionCompactionFence, fork_compaction_agent
        self.source_agent = agent
        self.agent, self.baseline = fork_compaction_agent(agent)
        self.fence = SessionCompactionFence(session, version)
        self.done = threading.Event()
        self._slot_lock = threading.Lock()
        self._slot_released = False

    def release_slot(self):
        from agent.conversation_compression import _release_compression_admission
        with self._slot_lock:
            if not self._slot_released:
                self._slot_released = True
                _release_compression_admission()
                _BETWEEN_TURNS_SLOT.release()


def _preempt_between_turns_compaction(session: dict) -> bool:
    """Cancellation detaches isolated work; only a commit winner is joined."""
    if session.get("_between_turns_compaction") is None:
        return False
    with session["history_lock"]:
        job = session.get("_between_turns_compaction")
    if job is None:
        return False
    if job.fence.cancel_before_commit(reason="preempted_by_turn"):
        from agent.between_turns_compaction import abandon_compaction_agent
        abandon_compaction_agent(job.agent)
        job.fence.revoke_commit_admission()
        with session["history_lock"]:
            if session.get("_between_turns_compaction") is job:
                session.pop("_between_turns_compaction", None)
    else:
        job.done.wait()  # DB commit already won: include in-memory publication.
    return True


def _run_between_turns_compaction(sid: str, session: dict, job, messages: list, version: int) -> None:
    agent = job.agent
    published = restart_worker = False
    host_snapshot = None
    try:
        with _session_profile_runtime_scope(session):
            tokens = _set_session_context(session.get("session_key") or "", cwd=_session_cwd(session))
            try:
                from agent.between_turns_compaction import between_turns_pressure
                pressure = between_turns_pressure(agent, messages)
                if pressure is None or job.fence.is_cancelled:
                    return
                # Cancel sources reach the protected provider-call seam even if a
                # response never returns. Explicit fence avoids a second host worker.
                from agent.conversation_compression import resolve_context_compression_timeouts
                _idle, ceiling = resolve_context_compression_timeouts()
                job.fence.set_total_ceiling_seconds(ceiling)
                def current():
                    with session["history_lock"]:
                        return (not job.fence.is_cancelled and not session.get("running")
                                and int(session.get("history_version", 0)) == version)
                # None, not the cached prompt: the caller system_message is folded into
                # the rebuild, so passing the old prompt nested it (doubled per compaction).
                after, _ = agent._compress_context(
                    messages, None, approx_tokens=pressure,
                    task_id=session.get("session_key") or "default", trigger="between_turns",
                    commit_fence=job.fence, snapshot_is_current=current,
                )
                if (after is messages or getattr(job.fence, "cancel_reason", None)
                        or not job.fence.publication_locked):
                    return
                # begin_commit retained history_lock through this publication.
                if (session.get("agent") is not job.source_agent
                        or int(session.get("history_version", 0)) != version):
                    return
                from agent.between_turns_compaction import publish_compaction_agent
                publish_compaction_agent(job.source_agent, agent, job.baseline)
                published = True
                session["history"] = after
                session["history_version"] = version + 1
                old_key = session.get("session_key")
                _sync_session_key_after_compress(
                    sid, session, clear_pending_title=False, restart_slash_worker=False)
                restart_worker = session.get("session_key") != old_key
                if os.environ.get("HERMES_COMPUTE_HOST_CHILD") == "1":
                    host_snapshot = {**session, "history": copy.deepcopy(after)}
            finally:
                _clear_session_context(tokens)
    except Exception:
        logger.exception("between-turns compaction failed")
    finally:
        job.fence.release_publication()
        job.release_slot()
        with session["history_lock"]:
            if session.get("_between_turns_compaction") is job:
                session.pop("_between_turns_compaction", None)
            job.done.set()
        if not published:
            from agent.between_turns_compaction import discard_compaction_agent
            discard_compaction_agent(agent)
    if published:
        # UI I/O and slash-worker attachment must not prolong the commit fence or
        # invert history_lock -> _sessions_lock against a concurrent close.
        with _session_profile_runtime_scope(session):
            if host_snapshot is not None:
                # The parent already received turn.end (or captured its older
                # snapshot). Publish the new identity without waiting for a turn.
                try:
                    from tui_gateway.compute_host import _history_meta
                    session["transport"].write({"jsonrpc": "2.0", "method": "compute_host.session_meta",
                        "params": {"sid": sid, **_history_meta(host_snapshot),
                                   "history": host_snapshot["history"],
                                   "session_info": _session_info(agent, host_snapshot)}})
                except Exception:
                    logger.exception("compute-host compaction metadata publication failed")
            if restart_worker:
                _restart_slash_worker(sid, session)
            _emit_session_info(sid, session)


def _start_between_turns_compaction(sid: str, session: dict, result: dict):
    """Called only after message.complete, persistence and followup scheduling.

    No ``running`` flag / turn marker / heartbeat: clients stay idle while summary
    work is in flight. Delegate/oneshot/kanban paths never call this TUI seam.
    """
    if not isinstance(result, dict) or not result.get("completed") or any(
        result.get(key) for key in ("interrupted", "failed", "error", "pending_steer")
    ):
        return None
    agent = session.get("agent")
    if not agent or getattr(agent, "compression_between_turns", False) is not True:
        return None
    from agent.conversation_compression import _try_admit_compression_job, _release_compression_admission
    with session["history_lock"]:
        if (session.get("running") or session.get("_closing") or session.get("queued_prompt")
                or session.get("_compute_host_active")
                or session.get("_manual_compress_active") or session.get("_between_turns_compaction")):
            return None
        # Do not launch on another path's lock. Core admission checks again under its fence.
        db = getattr(agent, "_session_db", None)
        if db is None or db.get_compression_lock_holder(agent.session_id):
            return None
        from agent.between_turns_compaction import between_turns_pressure
        messages = copy.deepcopy(session.get("history") or [])
        if between_turns_pressure(agent, messages) is None:
            return None
        version = int(session.get("history_version", 0))
        if not _BETWEEN_TURNS_SLOT.acquire(blocking=False):
            return None
        if not _try_admit_compression_job():
            _BETWEEN_TURNS_SLOT.release()
            return None
        try:
            job = _BetweenTurnsCompaction(agent, session, version)
        except BaseException:
            _release_compression_admission()
            _BETWEEN_TURNS_SLOT.release()
            raise
        session["_between_turns_compaction"] = job
    try:
        thread = _start_session_work(
            lambda: _run_between_turns_compaction(sid, session, job, messages, version),
            name=f"between-turns-compaction-{sid}")
        if thread is None:
            job.release_slot()
            with session["history_lock"]:
                if session.get("_between_turns_compaction") is job:
                    session.pop("_between_turns_compaction", None)
                job.done.set()
            return None
    except BaseException:
        job.release_slot()
        with session["history_lock"]:
            if session.get("_between_turns_compaction") is job:
                session.pop("_between_turns_compaction", None)
            job.done.set()
        raise
    return job


def register(server):
    bind_module(globals(), server, skip=("_",))
