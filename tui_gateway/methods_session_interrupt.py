"""Interrupt / steer / redirect session handlers (``methods_session`` split).

Moved verbatim from ``tui_gateway/methods_session.py`` (file-line ratchet #68779): the
bodies close over server.py globals through ``method_ctx.bind_module`` exactly as before —
``@method`` registration, ``_session_arg`` wrapping and module publication all still run
from the parent's ``register()`` via ``HandlerRegistry``.
"""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

def _resume_wake_after_interrupt() -> None:
    """Re-arm a wake lease held by the interrupt caller or a voice capture.

    ``_wake_resume_if_owner`` no-ops unless that object holds the lease, so an
    in-progress capture owned by someone else is not stolen. Interrupt already
    silenced TTS before it can return an error; this matches that cut. A
    ``not_interrupted`` hosted-task mismatch must not call it.
    """
    with _voice_sid_lock:
        voice_owner = _voice_wake_owner
    seen = []
    for owner in (_caller_transport(), voice_owner):
        if owner is None or any(owner is item for item in seen):
            continue
        seen.append(owner)
        _wake_resume_if_owner(owner)


# ── interrupt / steer / redirect ─────────────────────────────────────
def _note_user_input(session: dict) -> None:
    """Mark the running turn as touched by the user (also covers compute-host turns, whose agent
    flag never reaches this process); popped by the turn's own finally."""
    with session["history_lock"]:
        if session.get("running"):
            session["_turn_user_input"] = True


def _recover_unreachable_compute_host_turn(sid: str, session: dict, inflight_at_interrupt) -> None:
    """Stop on a compute host that is gone (broken pipe, respawn exhausted, #82603).

    The failed dial raised before ``_interrupt_session_turn`` purged the queue, and no ``turn.end`` will ever
    clear ``running``, so every later submit would queue forever. Clear the turn only when the supervisor owes
    no completion for it and the inflight dict is still the one this Stop saw (a successor turn keeps its own).
    The probe runs outside ``history_lock``: compute-host calls never run under it.
    """
    try:
        pending = _get_compute_host_supervisor().has_pending_turn(sid)
    except Exception as exc:  # health: allow BLE001 -- an unreadable supervisor is not proof the turn ended; fail closed
        logger.warning("session.interrupt: compute-host pending-turn probe failed for %s: %s", sid, exc)
        pending = True
    with session["history_lock"]:
        session["_turn_cancel_requested"] = True
        session["queued_prompt"] = None
        session.pop("queued_prompts", None)
        session["_queued_prompt_generation"] = int(session.get("_queued_prompt_generation", 0)) + 1
        inflight_now = session.get("inflight_turn")
        if not pending and session.get("running") and inflight_now is not None and inflight_now is inflight_at_interrupt:
            session["running"] = False
            _clear_inflight_turn(session)
    _clear_pending(sid)
    with contextlib.suppress(Exception):
        _announce_cancelled_gateway_approvals(session, "interrupt", session_id=sid)
        from tools.approval import resolve_gateway_approval
        resolve_gateway_approval(session["session_key"], "deny", resolve_all=True)


@method("session.interrupt")
def _(rid, params: dict) -> dict:
    if params.get("_mobile_expected_record") is not None:
        try:
            scoped = _mobile_scope(params["_mobile_scope_params"])[3]
            if scoped is not params["_mobile_expected_record"]:
                raise ValueError("runtime changed")
        except (ValueError, FileNotFoundError, KeyError):
            return _err(rid, 4400, "mobile runtime changed; reopen canonical Bot Chat")
    if params.get("_mobile_expected_record") is None:
        _tts_stream_stop()  # Desktop keypress barge-in silences process-global voice.
    resume_wake = True
    try:
        session, err = _sess_nowait(params, rid)
        if err:
            return err
        if expected := _str_param(params, "expected_hosted_task_id"):
            with session["history_lock"]:
                task = session.get("_hosted_room_task")
                if not (session.get("running") and isinstance(task, dict) and task.get("task_id") == expected):
                    resume_wake = False
                    return _ok(rid, {"status": "not_interrupted", "interrupted": False})
        sid = str(params.get("session_id") or "")
        if params.get("admission_id") is not None or any(
                r["receipt"].get("status") != "terminal" for r in session.get("_forwarded_admissions", {}).values()):
            return _interrupt_forwarded(rid, sid, session, params)
        _note_user_input(session)
        if _session_uses_compute_host(session):
            with session["history_lock"]:
                inflight_at_interrupt = session.get("inflight_turn")
            try:
                _interrupt_session_turn(sid, session, request_id=f"interrupt-{rid}")
            except Exception as exc:  # health: allow BLE001 -- the bridge raises transport-specific errors unknown here; logged, and Stop recovers the session instead of failing
                logger.warning("session.interrupt: compute-host interrupt failed for %s: %s", sid, exc)
                _recover_unreachable_compute_host_turn(sid, session, inflight_at_interrupt)
            return _ok(rid, {"status": "interrupted", "turn_isolation": True})
        # No agent wait: a turn parked on the deferred build already honors _turn_cancel_requested. Waiting here
        # failed Stop with 5032 once the build failed or outlived 30s (and started a build for a lazy session), so
        # the Desktop, which interrupts before session.close, could not delete such a session (#133262).
        _interrupt_session_turn(sid, session)
        # Retire the crash-recovery marker NOW: until the run thread's finally, a backend exit looks like a crash
        # and session.resume auto-continues the turn the user just stopped (the extra key covers compression
        # rotating session_key mid-turn).
        with session["history_lock"]:
            active_marker_key = str(session.pop("_active_turn_marker_key", "") or "")
        _retire_turn_marker(session, active_marker_key)
        return _ok(rid, {"status": "interrupted"})
    finally:
        if resume_wake:
            try:
                _resume_wake_after_interrupt()
            except Exception:
                logger.debug("session.interrupt wake resume failed", exc_info=True)


def _forwarded_control(rid, sid, session, verb, admission_id, generation=None):
    from tui_gateway.owner_forward import control_forwarded, pending_submissions
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli.gateway_client import GatewayRPCError
    try:
        result = control_forwarded(session, verb, admission_id, generation)
    except RuntimeStoreError as exc:
        return _err(rid, 4090, exc.reason, data={"reason": exc.reason})
    except GatewayRPCError as exc:
        return _err(rid, 4090, str(exc), data=exc.data)
    except (OSError, RuntimeError, TimeoutError) as exc:
        return _err(rid, 4090, str(exc), data={"reason": "SESSION_OWNER_UNCONFIRMED"})
    _emit("session.info", sid, {"stored_session_id": session.get("session_key"),
                                "pending_submissions": pending_submissions(session)})
    return _ok(rid, result)


def _interrupt_forwarded(rid, sid, session, params):
    """Stop only this window's forwarded admissions, never the unrelated owner turn ahead of them."""
    with session["history_lock"]:
        records = list(session.get("_forwarded_admissions", {}).items())
    selected = params.get("admission_id")
    if selected is not None:
        records = [(aid, row) for aid, row in records if aid == selected]
        if not records:
            return _err(rid, 4090, "not_found", data={"reason": "not_found"})
    for aid, record in records:
        status = record["receipt"].get("status")
        if status == "terminal":
            continue
        # Cancel's owner-side queued→started CAS handles a claim racing Stop. Once started was
        # observed, an explicit generation is checked instead of silently stopping a successor.
        verb = "session.interrupt" if status == "started" else "prompt.cancel"
        reply = _forwarded_control(rid, sid, session, verb, aid,
            params.get("execution_generation", record["receipt"].get("execution_generation")))
        if "error" in reply:
            return reply
    return _ok(rid, {"status": "interrupted"})


def _forwarded_method(name):
    @method(name)
    def _(rid, params):
        session, err = _sess_nowait(params, rid)
        if err:
            return err
        if not isinstance(params.get("admission_id"), str) or not params["admission_id"]:
            return _err(rid, 4002, "admission_id is required")
        if name == "prompt.resolve_unknown" and not isinstance(params.get("execution_generation"), int):
            return _err(rid, 4002, "execution_generation is required")
        return _forwarded_control(rid, str(params["session_id"]), session, name,
                                  params["admission_id"], params.get("execution_generation"))


for _forwarded_verb in ("prompt.cancel", "prompt.resolve_unknown", "prompt.receipt"):
    _forwarded_method(_forwarded_verb)


def _apply_correction(rid, session: dict, verb: str, text: str, accepted_status: str) -> dict:
    """``agent.<verb>(text)``; on acceptance record it on the live turn (mid-turn resume rebuilds the bubble)
    and purge queued self-copies so post-turn drain cannot re-fire the old prompt."""
    try:
        accepted = getattr(session["agent"], verb)(text)
    except Exception as exc:
        return _err(rid, 5000, f"{verb} failed: {exc}")
    if accepted:
        with session["history_lock"]:
            _record_inflight_correction(session, text)
            # #84417: steer does not cancel the live original, but a server queue self-copy of that original
            # must still not re-fire after settle (same class as redirect).
            # #84417: purge server-queue self-duplicates of the live original so post-turn drain cannot
            # restart the pre-correction prompt.
            _drop_queued_duplicates_of_inflight_user(session)
            session["last_active"] = time.time()
    return _ok(rid, {"status": accepted_status if accepted else "rejected", "text": text})


def _correction_method(name: str, verb: str, accepted_status: str, supported, unsupported: str):
    """steer/redirect RPC: ``params.text`` (4002, checked before the session) into a live session;
    ``supported(agent)`` gates 4010."""
    @method(name)
    def _(rid, params: dict) -> dict:
        if not (text := (params.get("text") or "").strip()):
            return _err(rid, 4002, "text is required")
        session, err = _sess_nowait(params, rid)
        if err:
            return err
        _note_user_input(session)
        agent = session.get("agent")
        # Redirect during the turn-build window (running=True, agent None): queue for the next turn instead of
        # a misleading 4010 the client swallows into a lost follow-up.
        if verb == "redirect" and agent is None and session.get("running"):
            return _queue_correction(rid, params["session_id"], session, text)
        # Compression in flight: queue instead of steering/redirecting. A correction that
        # reaches the provider mid-compression aborts the compression (explicit_interrupt)
        # — the follow-up kills the turn that would answer it (#61042). Queued here, it
        # drains when compression finishes (the Discord-gateway contract; mirrors the
        # interrupt→queue demotion in gateway/run_busy.py for the channel busy path).
        if _session_compression_in_flight(session):
            return _queue_correction(rid, params["session_id"], session, text)
        if not supported(agent):
            return _err(rid, 4010, unsupported)
        # An idle agent accepts steer() but only the next turn drains it, spliced after an old tool
        # row (#64578). 'rejected' makes the client queue it as a normal next prompt.
        if verb == "steer" and not session.get("running"):
            return _ok(rid, {"status": "rejected", "text": text})
        return _apply_correction(rid, session, verb, text, accepted_status)


def _queue_correction(rid, sid, session, text):
    reply = _handle_busy_submit(rid, sid, session, text, current_transport() or _stdio_transport,
                               queued=True, image_paths=[])
    if reply is None:
        return _ok(rid, {"status": "rejected", "text": text})
    if "result" in reply:
        reply["result"]["text"] = text
    return reply


# Inject text into the next tool result without interrupting (AIAgent.steer(): no new user turn, no role
# alternation violation).
_correction_method("session.steer", "steer", "queued", lambda agent: hasattr(agent, "steer"),
                   "agent does not support steer")
# Redirect the active model turn while preserving valid work/context.
_correction_method("session.redirect", "redirect", "redirected",
                   lambda agent: getattr(agent, "_supports_active_turn_redirect", False) is True
                   and hasattr(agent, "redirect"), "agent does not support active-turn redirect")


def register(server) -> None:
    """Publish this module's helpers onto ``server`` (rebound to its globals) and install handlers."""
    bind_module(globals(), server, skip=("_",))
