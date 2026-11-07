"""JSON-RPC admission and worker dispatch. Rebound onto the server namespace."""

from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor

from .method_ctx import bind_module


# A winning compaction can park one FIFO head. Keep a second worker available
# for controls in other chats, independently of slow shared-pool handlers.
_compaction_rpc_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tui-compaction-rpc")
atexit.register(lambda: _compaction_rpc_pool.shutdown(wait=False, cancel_futures=True))


def handle_request(req: dict) -> dict | None:
    from hermes_cli.backend_retirement import retirement

    with retirement.work() as admitted:
        if not admitted:
            return _err(req.get("id"), 5035, "backend is retiring; reconnect to continue")
        return _handle_admitted_request(req)


def _handle_admitted_request(req: dict) -> dict | None:
    normalized = _normalize_request(req)
    if isinstance(normalized, dict):
        return normalized
    rid, method, params = normalized
    if not (fn := _methods.get(method)):
        return _err(rid, -32601, _contracts.unknown_method_message(method))
    # Test doubles register straight into ``_methods`` without a contract; every production
    # handler comes through ``register_method`` and therefore has one.
    contract = _contracts.METHODS.get(method)
    if contract is not None:
        params, problem = _contracts.validate_params(contract, params)
        if problem is not None:
            code, message = contract.params_error or (4000, problem)
            return _err(rid, code, message)
    from hermes_cli.config_backend import ConfigBackendUnavailable

    token = _current_rpc_method.set(method)
    try:
        response = fn(rid, params)
    except ProfileUnavailableError as exc:
        return _err(rid, 4064, str(exc))
    except ConfigBackendUnavailable as exc:
        # A SystemExit on purpose (readers must not fall back to defaults), so no ``except
        # Exception`` below catches it: this request still owes its caller one answer.
        return _err(rid, 4064, str(exc.code))
    finally:
        _current_rpc_method.reset(token)
    if contract is not None and isinstance(response, dict) and isinstance(response.get("result"), dict):
        _contracts.check_params_accepted(contract, params)
        _contracts.check_result(contract, response["result"])
    return response


def _submit_compaction_session_rpc(session: dict, run, on_submit_error):
    """FIFO for formerly-inline RPCs; only the head occupies a pool worker.

    The session's commit may hold history_lock for plugin/network work. Queue
    here instead of parking the reader or filling the pool with same-chat waits.
    """
    from concurrent.futures import Future
    import threading

    future = Future()
    lock = session.setdefault("_between_turns_rpc_lock", threading.Lock())
    with lock:
        previous = session.get("_between_turns_rpc_tail")
        session["_between_turns_rpc_tail"] = future

    def complete():
        try:
            result = run()
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def start(_=None):
        try:
            _compaction_rpc_pool.submit(complete)
        except BaseException as exc:
            try:
                on_submit_error(exc)
            finally:
                future.set_exception(exc)

    if previous is None:
        start()
    else:
        previous.add_done_callback(start)
    return future


def dispatch(req: dict, transport: Optional[Transport] = None, *, on_caller_thread: bool = False) -> dict | None:
    """Route inbound RPCs — long handlers to the pool (returns None; the worker writes its own
    response via the bound transport), everything else inline (returns the response dict).
    *transport* pins every write of this request — events included — to that transport;
    omitted → the module stdio transport (``tui_gateway.entry`` behaviour). *on_caller_thread*: the
    caller is already a worker of its own, off every reader (a WebSocket read lane, bounded per
    socket), so a long handler runs here instead of taking a slot of the pool every client's long
    controls share; a session in between-turns compaction still queues on its FIFO."""
    t = transport or _stdio_transport
    token = bind_transport(t)
    try:
        from tui_gateway import server_requests
        if server_requests.is_response_frame(req):
            # The renderer answering one of OUR requests (clarify, approval, …): no response frame goes back.
            if not server_requests.resolve_response(req, t) and not _relay_compute_host_response(req):
                logger.debug("dropping response for unknown server request id=%r", req.get("id"))
            return None
        normalized = _normalize_request(req)
        if isinstance(normalized, dict):
            return normalized
        # An opted-in session can acquire its publication lock immediately after
        # this probe. Never run its history-locking RPCs on the socket reader:
        # an already-started commit (including plugin hooks) may be unbounded.
        sid = normalized[2].get("session_id")
        session = _sessions.get(sid) if isinstance(sid, str) else None
        pending_rpc = session.get("_between_turns_rpc_tail") if session is not None else None
        compaction_session = session is not None and (
            session.get("_between_turns_compaction") is not None
            or (pending_rpc is not None and not pending_rpc.done())
            or getattr(session.get("agent"), "compression_between_turns", False) is True)
        long_handler = normalized[1] in _LONG_HANDLERS
        if not long_handler and not compaction_session:
            return handle_request(req)
        if on_caller_thread and not compaction_session:
            try:
                return handle_request(req)
            # health: allow BLE001 -- the answer the long-handler pool gives a handler that raised (run() below)
            except Exception as exc:
                return _err(req.get("id"), -32000, f"handler error: {exc}")
        from hermes_cli.backend_retirement import retirement

        # Reserve BEFORE enqueueing: a queued handler has accepted work even though no worker runs yet.
        if not retirement.acquire():
            return _err(req.get("id"), 5035, "backend is retiring; reconnect to continue")
        try:
            ctx = contextvars.copy_context()  # the pool worker must see the bound transport
            owner = normalized[2].get("owner")
            if normalized[1] in _CONNECTOR_RPC_METHODS and isinstance(owner, dict) and owner.get("type") == "session":
                ctx.run(_capture_connector_rpc_owner, normalized[2])

            def run():
                try:
                    resp = _handle_admitted_request(req)
                except Exception as exc:
                    resp = _err(req.get("id"), -32000, f"handler error: {exc}")
                if resp is not None:
                    t.write(resp)
            if compaction_session and normalized[1] not in _LONG_HANDLERS:
                future = _submit_compaction_session_rpc(
                    session, lambda: ctx.run(run),
                    lambda exc: t.write(_err(req.get("id"), -32000, f"handler dispatch failed: {exc}")))
            else:
                future = _pool.submit(lambda: ctx.run(run))
        except BaseException:
            retirement.release()
            raise
        # Also releases cancelled queued futures; the worker's own finally would never execute.
        future.add_done_callback(lambda _: retirement.release())
        return None
    finally:
        reset_transport(token)


def register(server):
    bind_module(globals(), server)
