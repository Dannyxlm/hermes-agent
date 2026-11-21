"""Exact-admission thread controls; a stale tool observation never stops its successor."""
import asyncio
from dataclasses import asdict

from gateway.session_persistence import authority_persistence
from persistence.core.errors import AuthorityRefusal
from persistence.sqlite.conversation._store.launches import thread_launch


def _check_thread(authority, ref, control):
    from tools.thread_state import LAUNCH_PREFIX, tool_sent

    row = next((row for row in authority._pending_rows(ref)
                if row['admission_id'] == control['admission_id'] and row['status'] == 'started'), None)
    if row is None:
        raise AuthorityRefusal('stale_admission')
    if not (tool_sent(row) or control['force']):
        raise AuthorityRefusal('permission_denied')
    store = authority_persistence(authority)
    launch = thread_launch(store._bound_store().holder(), control['launch_id'])
    lineage = store.lineage.lineage(ref.session_id) or (ref.session_id,)
    if (not control['launch_id'].startswith(LAUNCH_PREFIX) or launch is None
            or launch['child_session_id'] not in lineage
            or launch['launcher_profile'] != control['launcher_profile']
            or launch['launcher_session_id'] != control['launcher_session_id']):
        raise AuthorityRefusal('permission_denied')
    return row


async def interrupt_thread(authority, actor, ref, generation, control):
    """Force relaxes the author check only, never the admission or generation fences."""
    authority.authorize(actor, ref, 'session:control')
    from pydantic import ValidationError
    from tui_gateway.contracts.canonical_sessions import CanonicalThreadControl
    try:
        control = CanonicalThreadControl.model_validate(control, strict=True).model_dump()
    except ValidationError as exc:
        raise AuthorityRefusal('invalid_params') from exc
    return asdict(await interrupt(authority, actor, ref, generation, control=control))


async def interrupt(authority, actor, ref, generation, *, control=None):
    """Exclude both FIFO claims and executor-thread adoption until the exact Stop effect returns.

    Caller cancellation cannot release the handoff while a provider cancellation still runs.
    The stream lock protects the identity/latches only, never the blocking agent call.
    """
    authority.authorize(actor, ref, 'session:control')
    live = authority.sessions[ref.session_id]

    async def deliver():
        async with live.claim_gate:
            if control is not None:
                with live.event_stream.lock:
                    authority.authorize(actor, ref, 'session:control')
                    if _check_thread(authority, ref, control)['generation'] != generation:
                        raise AuthorityRefusal('stale_generation')
                    from gateway.session_managed_worker import interrupt_managed
                    if interrupt_managed(authority, actor, ref, generation):
                        return authority._handle(ref)
            return await asyncio.to_thread(_interrupt, authority, actor, ref, generation, control)

    task = asyncio.create_task(deliver())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Retain the claim gate even when the RPC disappears. Observe any effect failure.
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        raise


def _interrupt(authority, actor, ref, generation, control):
    live = authority.sessions[ref.session_id]
    with live.control_handoff:
        with live.event_stream.lock:
            authority.authorize(actor, ref, 'session:control')
            if control is not None and _check_thread(authority, ref, control)['generation'] != generation:
                raise AuthorityRefusal('stale_generation')
            handle = authority._handle(ref)
            if handle.execution_generation != generation:
                raise AuthorityRefusal('stale_generation')
            agent = None
            if handle.execution_state == 'running':
                adopted_generation, agent = authority.adopted.get(ref.session_id, (None, None))
                if adopted_generation != generation:
                    returned = (live.event_stream.execution.get('execution_generation') == generation
                                and authority.executing.get(ref.session_id) != generation)
                    agent = None if returned else authority.agent(ref)
                    if not returned and not authority._runs_own_agent(ref):
                        authority.pending_stops[ref.session_id] = generation
                if agent is not None:
                    authority.delivered_stops[ref.session_id] = generation
        if agent is not None:
            from agent.interrupt_compat import request_hard_interrupt
            from persistence.sqlite.conversation._store.launch_causes import stamp_stop
            stamp_stop(agent, 'user_stop')
            request_hard_interrupt(agent)
        return authority._handle(ref)
