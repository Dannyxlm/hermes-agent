"""In-process session adapter for the hosted room driver: the room worker uses the same
installed session handlers as every TUI/Desktop turn (no WebSocket transport), passing the
task proof as an in-process-only Python object that JSON clients cannot forge."""

from __future__ import annotations

import contextlib
import itertools
import threading
from collections.abc import Mapping, Sequence
from types import ModuleType
from typing import Any, Callable

from gateway import hosted_room_driver as state
from tui_gateway.transport import bind_transport, reset_transport

_LockType = type(threading.Lock())


class _HostedRoomTransport:
    """A live internal sink for renderer-only session events.

    Hosted room results are committed through the fenced terminal callback and
    room log. Treating their hidden sessions as stdio clients can block a room
    worker behind an unread renderer pipe, while returning ``False`` would mark
    the session as disconnected. This sink deliberately accepts and discards
    those presentation frames.
    """

    __slots__ = ()

    def write(self, obj: dict) -> bool:
        del obj
        return True

    def close(self) -> None:
        return None


class HostedRoomSessionError(RuntimeError):
    """Raised when an in-process session operation is rejected."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed: {message}")
        self.method = method
        self.code = code


class HostedRoomServerRPC:
    """Normalize the installed server handlers for :class:`HostedRoomRuntime`."""

    def __init__(self, server: ModuleType) -> None:
        self.server = server
        self._ids = itertools.count(1)
        self._transport = _HostedRoomTransport()

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        token = bind_transport(self._transport)
        try:
            handler = self.server._methods[method]
            envelope = handler(f"hosted-room-{next(self._ids)}", params)
            error = envelope.get("error") if isinstance(envelope, dict) else None
            if isinstance(error, dict):
                raise HostedRoomSessionError(
                    method,
                    int(error.get("code") or 5000),
                    str(error.get("message") or "gateway rejected the request"),
                )
            result = envelope.get("result") if isinstance(envelope, dict) else None
            if not isinstance(result, dict):
                raise HostedRoomSessionError(method, 5000, "gateway returned no result")
            return result
        finally:
            reset_transport(token)

    def resolve_exact(self, *, profile: str, title: str, source: str) -> Mapping[str, Any] | None:
        del source
        result = self._call(
            "session.list", {"profile": profile, "title": title, "include_hidden": True})
        rows = result.get("sessions")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return None
        row = rows[0]
        return {"session_id": row.get("resolved_id") or row.get("id"),
                "title": row.get("title") or title}

    def create(self, *, profile: str, title: str, source: str) -> Mapping[str, Any]:
        return self._call("session.create", {
            "profile": profile, "title": title, "source": source, "hidden": True,
            "room_plumbing": True, "follow_profile_config": True, "close_on_disconnect": False})

    def resume(self, *, profile: str, session_id: str, source: str) -> Mapping[str, Any]:
        return self._call("session.resume", {
            "profile": profile, "session_id": session_id, "omit_messages": True, "source": source})

    def submit(
        self, *, profile: str, session_id: str, prompt: str, source: str, task: state.TaskIdentity,
        execution_generation: int, on_terminal: Callable[[Mapping[str, Any]], None], member_id: str,
    ) -> Mapping[str, Any]:
        try:
            return self._call("prompt.submit", {
                "profile": profile, "session_id": session_id, "text": prompt, "source": source,
                "_hosted_task": {
                    "room_id": task.room_id, "task_id": task.task_id, "thread_id": task.thread_id,
                    "turn_id": task.turn_id, "execution_generation": execution_generation,
                    "member_id": member_id},
                "_hosted_terminal_callback": on_terminal})
        except HostedRoomSessionError as exc:
            # In-process prompt.submit error envelopes come back before the background turn is
            # admitted; keep that proof so the driver can defer/requeue without an ambiguity lease.
            exc.not_admitted = True
            raise

    def history(self, *, profile: str, session_id: str, source: str) -> Sequence[Mapping[str, Any]]:
        del source
        result = self._call("session.history", {"profile": profile, "session_id": session_id})
        rows = result.get("messages")
        return tuple(row for row in rows if isinstance(row, dict)) if isinstance(rows, list) else ()

    def _session_record(self, session_id: str) -> dict[str, Any] | None:
        with self.server._sessions_lock:
            record = self.server._sessions.get(session_id)
            if record is not None:
                return record
            return next((c for c in self.server._sessions.values()
                         if str(c.get("session_key") or "") == session_id), None)

    def active_session_for(self, task: state.TaskIdentity) -> str | None:
        """Return the live runtime id only for this exact hosted task."""

        with self.server._sessions_lock:
            sessions = tuple(self.server._sessions.items())
        lock_type = type(threading.Lock())
        for session_id, record in sessions:
            lock = record.get("history_lock")
            if not isinstance(lock, lock_type):
                continue
            with lock:
                active = record.get("_hosted_room_task")
                if not record.get("running") or not isinstance(active, dict):
                    continue
                if all(
                    str(active.get(field) or "") == getattr(task, field)
                    for field in ("room_id", "task_id", "thread_id", "turn_id")
                ):
                    return str(session_id)
        return None


    def info(self, *, profile: str, session_id: str, source: str) -> Mapping[str, Any]:
        del profile, source
        record = self._session_record(session_id)
        if record is None:
            return {"active": False, "task_id": None}
        lock = record.get("history_lock")
        if not isinstance(lock, _LockType):
            return {"active": bool(record.get("running")), "task_id": None}
        with lock:
            task = record.get("_hosted_room_task")
            result = {"active": bool(record.get("running")),
                      "task_id": task.get("task_id") if isinstance(task, dict) else None}
            pending_reader = getattr(self.server, "_pending_approval_request_payload", None)
            if callable(pending_reader) and (pending := pending_reader(str(record.get("session_key") or ""))):
                result["status"] = "waiting_for_approval"
                from tui_gateway import server_requests
                projector = getattr(self.server, "_mobile_approvals", None)
                if callable(projector):
                    rows = projector(record, server_requests.open_requests(session_id))
                    pending = next((row for row in rows if row.get("request_id") == pending.get("request_id")), pending)
                result["pending_approval"] = pending
            return result

    def approve(self, *, session_id: str, request_id: str, choice: str,
                task_id: str | None = None, execution_generation: int | None = None,
                room_id: str | None = None, member_id: str | None = None, room_db_path=None,
                scoped_approval_response: int | None = None, request_epoch: str | None = None,
                request_revision: int | None = None, reason: str | None = None, auth: str | None = None,
                expected_authority_gateway_id: str | None = None,
                expected_authority_epoch: int | None = None) -> Mapping[str, Any]:
        """Never route room answers through approval.respond's stale-runtime fallback."""
        from tui_gateway.human_input import approval_response_metadata
        params = {key: value for key, value in {
            "scoped_approval_response": scoped_approval_response, "request_epoch": request_epoch,
            "request_revision": request_revision, "reason": reason, "auth": auth}.items() if value is not None}
        enhanced, _ = approval_response_metadata(params, choice)
        with self.server._session_resume_lock:
            record = self.server._sessions.get(session_id)
            if record is None:
                raise RuntimeError("local room approval runtime is unavailable")
            with record["history_lock"]:
                if self.server._sessions.get(session_id) is not record:
                    raise RuntimeError("local room approval runtime changed")
                with contextlib.ExitStack() as fence:
                    if enhanced:
                        task = record.get("_hosted_room_task") or {}
                        if (not room_db_path or not room_id or not member_id
                                or (task.get("room_id"), task.get("member_id"), task.get("task_id"), task.get("execution_generation"))
                                != (room_id, member_id, task_id, execution_generation)):
                            raise RuntimeError("local room approval task changed")
                        # Serialize authority/task mutations through the canonical room transaction
                        # until the exact approval commits. Cached matching alone cannot fence takeover.
                        fence.enter_context(state._transaction(room_db_path))
                        from tui_gateway.hosted_room_service import require_current_approval_authority
                        require_current_approval_authority(room_db_path, room_id, member_id, task_id,
                            execution_generation, expected_authority_gateway_id, expected_authority_epoch)
                    envelope = self.server._mobile_approval_respond(
                        f"hosted-room-{next(self._ids)}", record, request_id, choice,
                        params=params, expected_sid=session_id)
                    if "error" in envelope:
                        raise HostedRoomSessionError("room.approval", envelope["error"]["code"], envelope["error"]["message"])
                    return envelope["result"]

    def interrupt(
        self, *, profile: str, session_id: str, source: str, expected_task_id: str
    ) -> Mapping[str, Any] | None:
        del source
        return self._call("session.interrupt", {
            "profile": profile, "session_id": session_id,
            "expected_hosted_task_id": expected_task_id})
