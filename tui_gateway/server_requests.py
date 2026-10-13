"""Server→client JSON-RPC requests: the backend asks the renderer a question and waits for the
response frame carrying the same ``id``.

JSON-RPC is peer-to-peer; this is the backend's half. Every "ask the renderer" bridge (clarify,
approval, sudo, secret, vault prompts, desktop GUI reads, MCP setup consent, the tour) is one
:func:`send` (blocking) or :func:`send_async` (queue-backed approvals) and one response frame from
the client — no paired ``*.request`` notification / ``*.respond`` method, no per-kind ``*.expire``.

Ids are ``srq-<12 hex>``: strings never collide with client-minted integer ids, and the random
part keeps a compute-host child's requests distinct from the parent's when both reach one socket.
A request that times out or is cancelled (interrupt, session close, shutdown) emits ONE
``request.cancel {id, method, reason}`` notification so every renderer tears the card down the
same way. A response for an id that is no longer open is dropped — the wait already returned.

Reconnect: unanswered requests are returned as ``open_requests`` by ``session.resume`` /
``session.activate`` / ``session.events.since`` (:func:`open_requests`); the shared TypeScript
channel re-delivers them as if they had just arrived, so the notification replay ring never
has to carry "a question still waiting for an answer".

Batch clarify keeps per-question locks (``clarify.lock`` → :func:`lock_answer`): answers stay
editable until every question is locked, locked answers survive a timeout, and the last lock
resolves the request with the full answer set.

Window-owned bridges (``preview.read`` / ``preview.act`` / ``terminal.read`` / ``window.read`` /
``tour``) are answered only by the window showing the session; every other attached window declines
with :data:`NOT_SHOWN_CODE`. A decline does not settle the request — the owner may still answer — until
every answering client attached to the session has declined; then the request resolves with
:data:`NOT_SHOWN_MESSAGE` at once instead of the agent waiting out the deadline (#119333).

Capability: a client says once per connection that it answers server→client requests
(``client.capabilities {server_requests: true}`` → :func:`advertise`). A WebSocket client that never
did is a build older than this half of the protocol — it drops the frame silently and the agent
would wait the full deadline (clarify's 300s) for nothing — so :func:`send` / :func:`send_async`
return the same ``None`` an error response produces without writing the frame (#112548).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ServerRequest:
    __slots__ = (
        "answered",
        "created_at",
        "deadline_monotonic",
        "declined",
        "event",
        "expires_at",
        "id",
        "locked",
        "method",
        "on_result",
        "params",
        "profile_home",
        "qids",
        "result",
        "revision",
        "sid",
        "timeout_seconds",
    )

    def __init__(self, sid: str, method: str, params: dict, *, qids: list[str] | None = None,
                 on_result: Callable[[dict | None], None] | None = None,
                 timeout: float | None = None, deadline_monotonic: float | None = None,
                 created_at: float | None = None) -> None:
        self.id = f"srq-{uuid.uuid4().hex[:12]}"
        self.sid = sid
        self.method = method
        self.params = dict(params)
        self.event = threading.Event()
        self.result: dict | None = None
        self.answered = False
        from hermes_constants import get_hermes_home
        from pathlib import Path
        self.profile_home = str(Path(get_hermes_home()).resolve())
        self.created_at = time.time() if created_at is None else created_at
        self.timeout_seconds = timeout
        self.deadline_monotonic = (deadline_monotonic if deadline_monotonic is not None else
                                   time.monotonic() + timeout if timeout is not None else None)
        self.expires_at = self.created_at + timeout if timeout is not None else None
        self.revision = 1
        # Batch clarify: question ids still to lock, and the answers locked so far.
        self.qids = list(qids) if qids else ([q["qid"] for q in params.get("questions", [])] or None if method == "clarify" else None)
        self.locked: dict[str, str | None] = {}
        self.on_result = on_result
        # Client transports that answered NOT_SHOWN_CODE (no window there shows this session).
        self.declined: set = set()

    def frame(self) -> dict:
        return {"jsonrpc": "2.0", "id": self.id, "method": self.method,
                "params": self.wire_params()}

    def wire_params(self) -> dict:
        from .human_input import SECURE_METHODS, approval_quick_once, safe_params
        params = safe_params(self.method, self.params) if self.method in SECURE_METHODS else self.params
        if self.method == "approval":
            params = {**params, "quick_once": approval_quick_once(params), "risk": params.get("risk", "unknown")}
        return {**params, "session_id": self.sid, "created_at": self.created_at,
                "expires_at": self.expires_at, "timeout_seconds": self.timeout_seconds,
                "server_time": time.time(), "request_epoch": inbox_epoch(), "request_revision": self.revision}

    def expired(self) -> bool:
        return self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic

    def snapshot(self) -> dict:
        """``open_requests`` entry: the request as sent, plus the batch answers locked so far so a
        reconnecting client restores its ✓ state."""
        params = self.wire_params()
        if self.locked:
            params["answers"] = dict(self.locked)
        return {"id": self.id, "method": self.method, "params": params}


_lock = threading.Lock()
_open: dict[str, ServerRequest] = {}
# Bounded metadata-only reconciliation caches, never answer values or value digests.
_terminal: dict[str, dict] = {}
_receipts: dict[tuple, dict] = {}
_MAX_RECEIPTS = 4096

# Frame sinks, bound by ``bind_sinks`` from server.py at import time (like the method_ctx split
# modules): importing server back from here would pick a different module object under the test
# fixtures that patch ``sys.modules`` around the server import.
_write: Callable[[dict], Any] = lambda frame: None
_emit: Callable[[str, str, dict], Any] = lambda event, sid, payload: None
# ``answerable(sid)``: False only when every client attached to the session is a build that never
# advertised handling server→client requests (session_transports.py::_session_client_answers_requests).
_answerable: Callable[[str], bool] = lambda sid: True
# ``clients(sid)``: the attached client transports that answer server→client requests — the set whose
# unanimous NOT_SHOWN_CODE decline settles a window-owned request (session_transports.py).
_clients: Callable[[str], list] = lambda sid: []

# Error code a client answers when none of its windows shows the request's session, and the refusal the
# tool reports once every attached client said so. Mirrored in apps/desktop server-requests.ts.
NOT_SHOWN_CODE = 4404
NOT_SHOWN_MESSAGE = ("No Hermes Desktop window is showing this chat, so its preview, terminal and tour are out "
                     "of reach. Ask the user to open this chat in the Desktop app, then retry.")

# Client transports that sent ``client.capabilities {server_requests: true}`` (identity set: StdioTransport
# has __slots__ and cannot be weak-referenced; ws.py forgets a peer on disconnect).
_answering_clients: set = set()

# ``listener(sid)`` runs when an Inbox-kind request (approval, clarify, input) opens or settles: the
# session change cursor and the Inbox silent-push hint follow attention that the session table never
# shows. Called outside the lock; a failing listener is logged and skipped.
_attention_listeners: list[Callable[[str], None]] = []


def add_attention_listener(listener: Callable[[str], None]) -> None:
    with _lock:
        if listener not in _attention_listeners:
            _attention_listeners.append(listener)


def _attention_changed(req: ServerRequest) -> None:
    if req.method not in _INBOX_KINDS:
        return
    for listener in list(_attention_listeners):  # noqa: PERF101 - listeners may mutate registration
        try:
            listener(req.sid)
        except Exception:
            logger.debug("attention listener failed for %s", req.method, exc_info=True)


def bind_sinks(write_json: Callable[[dict], Any], emit: Callable[[str, str, dict], Any],
               answerable: Callable[[str], bool], clients: Callable[[str], list] | None = None) -> None:
    global _write, _emit, _answerable, _clients
    _write, _emit, _answerable = write_json, emit, answerable
    if clients is not None:
        _clients = clients


def advertise(transport: Any, server_requests: bool) -> None:
    """Record whether *transport*'s client answers server→client requests (``client.capabilities``)."""
    with _lock:
        if server_requests:
            _answering_clients.add(transport)
        else:
            _answering_clients.discard(transport)


def forget(transport: Any) -> None:
    """Drop a disconnected transport's advertisement."""
    with _lock:
        _answering_clients.discard(transport)


def answers_requests(transport: Any) -> bool:
    with _lock:
        return transport in _answering_clients


def _unanswerable(method: str, sid: str) -> bool:
    if _answerable(sid):
        return False
    logger.info("server request %s for %s not sent: the attached client predates server→client requests "
                "(update the Hermes app)", method, sid)
    return True


def _emit_cancel(req: ServerRequest, reason: str) -> None:
    _emit("request.cancel", req.sid, {"id": req.id, "method": req.method, "reason": reason})


def _register(req: ServerRequest) -> None:
    from tui_gateway.contracts import registry as contracts

    contract = contracts.SERVER_REQUESTS.get(req.method)
    if contract is None:
        raise RuntimeError(f"server request {req.method!r} has no contract in tui_gateway/contracts")
    from pydantic import ValidationError

    try:
        contract.params.model_validate({"session_id": req.sid, **req.params})
    except ValidationError:
        # Producer input can contain secure values: never echo validation details.
        raise ValueError("Invalid server request params") from None
    with _lock:
        _open[req.id] = req
    _attention_changed(req)
    _write(req.frame())


def send(method: str, sid: str, params: dict, *, timeout: float | None,
         qids: list[str] | None = None) -> dict | None:
    """Send one request and block for the response ``result`` (a dict).

    Returns ``None`` when the renderer never answered (timeout, cancel, or an error response — e.g.
    a client without a handler for ``method``). ``timeout`` semantics: None → wait until answered or
    cancelled, 0 → return immediately, > 0 → bounded wait. A batch (``qids``) that settled returns
    ``{"answers": <locked so far>, "outcome"}`` (``submitted`` / ``cancelled`` / ``timed_out``).
    """
    if _unanswerable(method, sid):
        return None
    req = ServerRequest(sid, method, params, qids=qids, timeout=timeout)
    _register(req)
    try:
        req.event.wait(None if req.deadline_monotonic is None else max(0, req.deadline_monotonic - time.monotonic()))
    except BaseException:
        # The wait itself died (KeyboardInterrupt, SystemExit, injected error): withdraw the request
        # or it stays in _open forever — replayed to every reconnecting client and reported by
        # pending_kind() as a human still being waited on.
        with _lock:
            still_open = _open.pop(req.id, None) is req
            if still_open:
                _remember_terminal(req, "cancelled")
        if still_open:
            _attention_changed(req)
            _emit_cancel(req, "interrupted")
        raise
    with _lock:
        # The verdict is the state committed under the lock, never wait()'s return value: a
        # response frame can land after the deadline expires and before this removal, and
        # settlement (resolve_response / lock_answer / cancel) already popped it (#112548).
        timed_out = _open.pop(req.id, None) is req
        if timed_out:
            _remember_terminal(req, "expired")
        answered, result, locked = req.answered, req.result, dict(req.locked)
    if answered:
        return result
    if timed_out:
        _attention_changed(req)
        _emit_cancel(req, "timeout")
        if req.qids is not None:
            return {"answers": locked, "outcome": "timed_out"}
    return None


def send_async(method: str, sid: str, params: dict, on_result: Callable[[dict | None], None], *,
               timeout: float | None = None, deadline_monotonic: float | None = None,
               created_at: float | None = None) -> Callable[[str], None]:
    """Send one request whose wait is owned elsewhere (the approval queue's own timeout). ``on_result``
    runs on the dispatching thread when the response lands. Returns ``settle(reason)``: call it when
    the underlying wait ends; if the request is still open it is withdrawn with ``request.cancel``."""
    if _unanswerable(method, sid):
        on_result(None)
        return lambda reason: None
    req = ServerRequest(sid, method, params, on_result=on_result, timeout=timeout,
                        deadline_monotonic=deadline_monotonic, created_at=created_at)
    _register(req)

    def settle(reason: str) -> None:
        with _lock:
            still_open = _open.pop(req.id, None) is not None
            if still_open:
                _remember_terminal(req, "expired" if reason == "timeout" else
                                   "already_resolved" if reason == "resolved" else "cancelled")
        if still_open:
            _attention_changed(req)
            _emit_cancel(req, reason)

    return settle


def _is_not_shown(frame: dict) -> bool:
    error = frame.get("error")
    return isinstance(error, dict) and error.get("code") == NOT_SHOWN_CODE


def _decline(rid: str, transport: Any, *, expected_sid: str | None = None,
             expected_method: str | None = None) -> bool:
    """One client's "no window here shows this session". Settles only once every answering client
    attached to the session declined: a bystander window must not beat the owner (#113348), and with
    no owner at all the agent gets the refusal now rather than at the deadline (#119333). A decline
    from an unknown transport (relayed, proxied) is recorded nowhere and the wait goes on."""
    with _lock:
        req = _open.get(rid)
    if req is None:
        logger.debug("server request %s: decline dropped, request no longer open", rid)
        return False
    clients = set(_clients(req.sid)) if transport is not None else set()
    with _lock:
        if _open.get(rid) is not req:
            return True  # settled meanwhile
        _check_owner(req, expected_sid, expected_method)
        if transport is not None:
            req.declined.add(transport)
        if not clients or not clients <= req.declined:
            return True
        _open.pop(rid, None)
        req.result = {"value": json.dumps({"success": False, "error": NOT_SHOWN_MESSAGE})}
        req.answered = True
        _remember_terminal(req, "cancelled")
    _attention_changed(req)
    if req.on_result is not None:
        req.on_result(req.result)
    req.event.set()
    return True


def _remember_terminal(req: ServerRequest, status: str) -> None:
    _terminal[req.id] = {"status": status, "session_id": req.sid, "method": req.method,
                         "profile_home": req.profile_home, "request_epoch": inbox_epoch(),
                         "request_revision": req.revision}
    while len(_terminal) > _MAX_RECEIPTS:
        _terminal.pop(next(iter(_terminal)))


def _consume(req: ServerRequest, result: dict | None, status: str) -> None:
    # Caller owns _lock. No callback or external IO under the registry lock.
    _open.pop(req.id, None)
    req.result, req.answered = result, status == "accepted"
    _remember_terminal(req, "outcome_unknown" if status == "accepted" and req.on_result else status)


def _deliver(req: ServerRequest, status: str) -> str:
    _attention_changed(req)
    try:
        if req.on_result is not None:
            consumed = req.on_result(req.result)
            if status == "accepted" and consumed is False:
                status = "already_resolved"
    except Exception:  # health: allow BLE001 -- callback exceptions may contain entered credentials; log only the method
        # Even exception text can contain a secure answer. Never echo/log it.
        status = "outcome_unknown"
        logger.warning("human input callback outcome unknown (%s)", req.method)
    finally:
        req.event.set()
    with _lock:
        _remember_terminal(req, status)
    _emit_cancel(req, "resolved" if status in ("accepted", "already_resolved") else
                 "timeout" if status == "expired" else "interrupted")
    return status


def _answer_result(req: ServerRequest, result: dict) -> dict:
    from .human_input import validate_answer
    result = validate_answer(req.method, req.params, result)
    if req.qids:
        answers = result.get("answers")
        # A lock accepted elsewhere must not be overwritten by a stale full reply.
        if answers is not None:
            result = {"answers": {**answers, **req.locked}, "outcome": "submitted"}
        else:
            result = {"answers": dict(req.locked), "outcome": "cancelled"}
    return result


def resolve_response(frame: dict, transport: Any = None, *, expected_sid: str | None = None,
                     expected_method: str | None = None, mobile: bool = False) -> bool:
    """One validated settlement for Desktop, WS and scoped HTTP. Legacy frames need no new fields."""
    rid = frame.get("id")
    if not isinstance(rid, str):
        return False
    if _is_not_shown(frame):
        return _decline(rid, transport, expected_sid=expected_sid, expected_method=expected_method)
    with _lock:
        req = _open.get(rid)
        if req is None:
            return False
        _check_owner(req, expected_sid, expected_method)
        if req.expired():
            status, result = "expired", None
        elif "error" in frame:
            status, result = "cancelled", None
        else:
            result = _answer_result(req, frame.get("result"))
            if mobile:
                if req.method == "approval" and result.get("choice") not in ("once", "deny"):
                    raise PermissionError("Approval choice is not offered to mobile")
                if req.qids is not None:
                    raise ValueError("question_id required for batch clarification")
            status = "accepted"
        _consume(req, result, status)
    _deliver(req, status)
    return status != "expired"


def _check_owner(req: ServerRequest, sid: str | None, method: str | None) -> None:
    if (sid is not None and req.sid != sid) or (method is not None and req.method != method):
        raise PermissionError("pending request does not belong to this runtime")


def _lock_question(req: ServerRequest, question_id: str, answer: str | None) -> list[str]:
    if req.qids is None or question_id not in req.qids:
        raise ValueError("Unknown clarification question")
    if answer is not None and (not isinstance(answer, str) or len(answer.encode()) > 65536):
        raise ValueError("Invalid clarification answer")
    if question_id not in req.locked or req.locked[question_id] != answer:
        req.locked[question_id] = answer
        req.revision += 1
    remaining = [qid for qid in req.qids if qid not in req.locked]
    if not remaining:
        _consume(req, {"answers": dict(req.locked), "outcome": "submitted"}, "accepted")
    return remaining


def lock_answer(request_id: str, question_id: str, answer: str | None, *,
                expected_sid: str | None = None) -> list[str] | None:
    with _lock:
        req = _open.get(request_id)
        if req is None:
            return None
        _check_owner(req, expected_sid, "clarify")
        if req.expired():
            _consume(req, None, "expired")
            remaining, status = None, "expired"
        else:
            remaining = _lock_question(req, question_id, answer)
            status = "accepted"
    if not remaining:
        _deliver(req, status)
    else:
        _attention_changed(req)
    return remaining


def cancel(sid: str | None = None, reason: str = "interrupted") -> int:
    """Withdraw open requests — only *sid*'s (session.interrupt must not touch other sessions'), or
    every one when *sid* is None (shutdown). Blocked waits return None (a batch returns its locked
    answers with ``outcome: cancelled``); queue-backed requests run ``on_result(None)`` so their
    owner can settle. Returns the number withdrawn."""
    with _lock:
        targets = [req for req in _open.values() if sid is None or req.sid == sid]
        for req in targets:
            _remember_terminal(req, "expired" if reason == "timeout" else "cancelled")
            _open.pop(req.id, None)
            if req.qids is not None:
                req.result, req.answered = {"answers": dict(req.locked), "outcome": "cancelled"}, True
            else:
                req.result, req.answered = None, False
    for req in targets:
        _attention_changed(req)
        if req.on_result is not None:
            req.on_result(None)
        req.event.set()
        _emit_cancel(req, reason)
    return len(targets)


def open_requests(sid: str) -> list[dict]:
    """Unanswered requests for *sid*, oldest first."""
    with _lock:
        reqs = sorted((req for req in _open.values() if req.sid == sid), key=lambda r: r.created_at)
        return [req.snapshot() for req in reqs]


def has_pending_human(sid: str) -> bool:
    from .human_input import HUMAN_METHODS
    with _lock:
        return any(req.sid == sid and req.method in HUMAN_METHODS and not req.expired() for req in _open.values())


def scoped_pending(sid: str, *, profile_home: str) -> list[dict]:
    from .human_input import HUMAN_METHODS, safe_params
    with _lock:
        requests = sorted((req for req in _open.values() if req.sid == sid
                           and req.profile_home == profile_home and req.method in HUMAN_METHODS
                           and not req.expired()), key=lambda req: req.created_at)
        return [{**req.snapshot(), 'params': safe_params(req.method, req.snapshot()['params'])} for req in requests]


def _receipt_selector(body: dict) -> tuple:
    return tuple(body.get(key) for key in ('request_id', 'method', 'session_id', 'request_epoch',
                                         'request_revision', 'question_id'))


def _receipt(body: dict, status: str) -> dict:
    return {'protocol_version': 1, 'status': status, 'action_id': body['action_id'],
            'request_id': body['request_id'], 'method': body['method'],
            'request_epoch': body['request_epoch'], 'request_revision': body['request_revision'],
            'server_time': time.time()}


def remember_remote_receipt(body: dict, *, scope: tuple, receipt: dict) -> None:
    from .contracts.human_input import HumanInputReceipt
    from pydantic import ValidationError
    try:
        metadata = HumanInputReceipt.model_validate(receipt).model_dump(mode='json')
    except ValidationError:
        raise ValueError('Invalid action receipt') from None
    for field in ('action_id', 'request_id', 'method', 'request_epoch', 'request_revision'):
        if metadata[field] != body[field]:
            raise ValueError('Mismatched action receipt')
    if metadata['status'] not in ('accepted', 'already_resolved', 'expired', 'cancelled', 'conflict'):
        return
    with _lock:
        _receipts[(*scope, body['action_id'])] = {'selector': _receipt_selector(body), 'receipt': metadata}
        while len(_receipts) > _MAX_RECEIPTS:
            _receipts.pop(next(iter(_receipts)))


def action_status(body: dict, *, scope: tuple) -> dict:
    with _lock:
        entry = _receipts.get((*scope, body['action_id']))
        if entry is not None:
            if entry['selector'] != _receipt_selector(body):
                return _receipt(body, 'conflict')
            return dict(entry['receipt'])
    return _receipt(body, 'unknown')


def _scoped_state(req: ServerRequest | None, body: dict, scope: tuple) -> str:
    if req is not None:
        _check_owner(req, body['session_id'], body['method'])
        if req.profile_home != scope[-1]:
            raise PermissionError('Request profile mismatch')
        if body['request_epoch'] != inbox_epoch() or body['request_revision'] != req.revision:
            return 'conflict'
        return 'expired' if req.expired() else 'pending'
    terminal = _terminal.get(body['request_id'])
    if body['request_epoch'] != inbox_epoch():
        return 'conflict'
    if terminal is None:
        return 'unknown'
    if (terminal['profile_home'], terminal['session_id'], terminal['method']) != (scope[-1], body['session_id'], body['method']):
        raise PermissionError('Request ownership mismatch')
    return 'already_resolved' if terminal['status'] == 'accepted' else terminal['status']


def scoped_respond(body: dict, *, scope: tuple) -> dict:
    from .human_input import HUMAN_METHODS
    deliver = False
    with _lock:
        key = (*scope, body['action_id'])
        previous = _receipts.get(key)
        if previous is not None:
            return (dict(previous['receipt']) if previous['selector'] == _receipt_selector(body)
                    else _receipt(body, 'conflict'))
        req = _open.get(body['request_id'])
        status = _scoped_state(req, body, scope)
        if req is not None and status == 'pending':
            if req.method not in HUMAN_METHODS:
                raise PermissionError('Not a human input request')
            if body.get('question_id') is not None:
                if req.method != 'clarify' or not isinstance(body['result'], dict) or set(body['result']) != {'answer'}:
                    raise ValueError('Invalid question answer')
                remaining = _lock_question(req, body['question_id'], body['result']['answer'])
                deliver = not remaining
            else:
                result = _answer_result(req, body['result'])
                _consume(req, result, 'accepted')
                deliver = True
            status = 'accepted'
        elif req is not None and status == 'expired':
            _consume(req, None, 'expired')
            deliver = True
        receipt = _receipt(body, 'outcome_unknown' if deliver and req.on_result else status)
        _receipts[key] = {'selector': _receipt_selector(body), 'receipt': receipt}
        while len(_receipts) > _MAX_RECEIPTS:
            _receipts.pop(next(iter(_receipts)))
    if deliver:
        status = _deliver(req, status)
        with _lock:
            receipt = _receipt(body, status)
            if key in _receipts:
                _receipts[key]['receipt'] = receipt
    elif req is not None and status == 'accepted':
        _attention_changed(req)
    return dict(receipt)


def open_request_count() -> int:
    """Unanswered server→client requests across every session: the process is waiting on a
    human (clarify, approval, sudo, secret, ...) and must not be treated as idle."""
    with _lock:
        return len(_open)


_INBOX_KINDS = {
    "approval": "approval", "clarify": "clarify", "sudo": "input", "secret": "input",
    "vault.unlock_prompt": "input", "vault.save_login": "input", "vault.code": "input",
    "display.install.sudo": "input", "setup_choose": "input",
}
_INBOX_EPOCH = uuid.uuid4().hex


def inbox_epoch() -> str:
    return _INBOX_EPOCH


def attention_summaries(sids: list[str]) -> dict[str, dict]:
    """Atomic metadata-only cut. GUI reads/tours are not human-input evidence.

    Revision is an opaque equality token for the supported request set, not an
    ordering watermark. Request ids keep it distinct across process restarts.
    """
    import hashlib
    admitted = set(sids)
    with _lock:
        grouped: dict[str, list] = {}
        for req in _open.values():
            if req.sid in admitted and req.method in _INBOX_KINDS:
                grouped.setdefault(req.sid, []).append((req.id, _INBOX_KINDS[req.method]))
    priority = {"approval": 0, "clarify": 1, "input": 2}
    return {sid: {
        "kind": min((kind for _, kind in reqs), key=priority.__getitem__),
        "count": len(reqs),
        "revision": hashlib.sha256(json.dumps(sorted(reqs)).encode()).hexdigest(),
    } for sid, reqs in grouped.items()}


def pending_kind(sid: str) -> str:
    """Method of the oldest open request for *sid* ("" when none) — the session is waiting on a human."""
    with _lock:
        reqs = [req for req in _open.values() if req.sid == sid]
    return min(reqs, key=lambda r: r.created_at).method if reqs else ""


def is_response_frame(obj: Any) -> bool:
    """A client response: has an ``id`` and a ``result``/``error`` member but no ``method``."""
    return isinstance(obj, dict) and "method" not in obj and "id" in obj and ("result" in obj or "error" in obj)


def reset_for_tests() -> None:
    with _lock:
        _open.clear()
        _terminal.clear()
        _receipts.clear()
        _answering_clients.clear()
