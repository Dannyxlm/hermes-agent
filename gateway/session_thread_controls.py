"""Exact-admission thread controls; a stale tool observation never stops its successor."""
from dataclasses import asdict

from hermes_state_runtime import RuntimeStoreError


def _check_thread(authority, ref, control):
    from tools.thread_state import LAUNCH_PREFIX, tool_sent

    row = next((row for row in authority._pending_rows(ref)
                if row['admission_id'] == control['admission_id'] and row['status'] == 'started'), None)
    if row is None:
        raise RuntimeStoreError('stale_admission')
    if not (tool_sent(row) or control['force']):
        raise RuntimeStoreError('permission_denied')
    launch = authority.db._read_one(
        'SELECT child_session_id, launcher_profile, launcher_session_id FROM work_launches WHERE launch_id=?',
        (control['launch_id'],))
    lineage = authority.db.get_compression_lineage(ref.session_id) or [ref.session_id]
    if (not control['launch_id'].startswith(LAUNCH_PREFIX) or launch is None
            or launch['child_session_id'] not in lineage
            or launch['launcher_profile'] != control['launcher_profile']
            or launch['launcher_session_id'] != control['launcher_session_id']):
        raise RuntimeStoreError('permission_denied')
    return row


async def interrupt_thread(authority, actor, ref, generation, control):
    """Check launch ownership, author, admission and generation at the owner, before either stop path.

    The claim gate excludes an in-flight/new claim; the stream lock pairs the running admission
    with agent adoption. No await below the checks yields before the interrupt is delivered/latched.
    Force relaxes the author check only, never the admission or generation fences.
    """
    authority.authorize(actor, ref, 'session:control')
    from pydantic import ValidationError
    from tui_gateway.contracts.canonical_sessions import CanonicalThreadControl
    try:
        control = CanonicalThreadControl.model_validate(control, strict=True).model_dump()
    except ValidationError as exc:
        raise RuntimeStoreError('invalid_params') from exc
    live = authority.sessions[ref.session_id]
    async with live.claim_gate:
        with live.event_stream.lock:
            authority.authorize(actor, ref, 'session:control')
            row = _check_thread(authority, ref, control)
            if row['generation'] != generation or authority._handle(ref).execution_generation != generation:
                raise RuntimeStoreError('stale_generation')
            from gateway.session_managed_worker import interrupt_managed
            if interrupt_managed(authority, actor, ref, generation):
                return asdict(authority._handle(ref))
            return asdict(await authority.interrupt(actor, ref, generation))
