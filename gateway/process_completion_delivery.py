"""Ordinary-process obligations use the existing session automation FIFO and receipts."""
from tools.process_registry_delivery import admission_process_events, notification_state, settle_notification


def acknowledge_process_admission(event, receipt):
    for member in admission_process_events(event):
        settle_notification(member, 'admitted', admission_id=receipt.admission_id, reason='fifo_accepted')


def reconcile_process_admission(runner, event):
    from gateway.session_automation import completion_admission
    row = completion_admission(runner, event)
    if row is not None:
        settle_notification(event, 'admitted', admission_id=row['admission_id'], reason='fifo_receipt')
    return row


async def deliver_process_completion(runner, text, event, *, sibling_claims=()):
    """Reconcile before target classification: even /new cannot undo an accepted FIFO input.

    An unknown request with no receipt stays unknown. No timeout, empty lookup, retry
    budget or in-memory dedup marker authorizes repeating its side effects.
    """
    state = notification_state(event)
    if state is None:
        return await runner._deliver_completion_notification_scoped(text, event, sibling_claims=sibling_claims)
    if state['status'] in {'admitted', 'terminal'}:
        return None
    from gateway.session_local_recovery import restore_completion_route
    restore_completion_route(runner, str(event.get('session_key') or ''))
    from gateway.session_detached import follow_detached_pin
    event = await follow_detached_pin(runner, event)
    if reconcile_process_admission(runner, event) is not None:
        return True
    if state['status'] == 'unknown':
        return False
    from tools.process_registry import process_registry
    from tools.process_registry_notifications import should_surface_notification
    if (not should_surface_notification(event)
            or process_registry.is_completion_consumed(event['session_id'])):
        settle_notification(event, 'terminal', reason='suppressed_or_consumed')
        return None
    parent = str(event.get('parent_session_id') or '')
    if parent:
        verdict = await runner._classify_completion_target(parent, str(event.get('session_key') or ''))
        if verdict == 'terminal':
            settle_notification(event, 'terminal', reason='target_gone')
            return None
        if verdict == 'retry':
            return False
    # A legacy adapter has no queryable FIFO receipt. Keep the obligation pending,
    # rather than declare a volatile callback a durable acceptance.
    from gateway.session_authorities import active_authority
    if active_authority(runner) is None:
        return False
    return await runner._deliver_completion_notification_scoped(text, event, sibling_claims=sibling_claims)


async def drain_recovered_process_completions(runner, target_queue):
    """Recovered processes no longer have polling watchers. The existing idle drain owns them."""
    events, keep = [], []
    while not target_queue.empty():
        event = target_queue.get_nowait()
        (events if event.get('_process_completion_recovery') else keep).append(event)
    for event in keep:
        target_queue.put(event)
    from tools.process_registry_notifications import format_process_notification
    retry = []
    for index, event in enumerate(events):
        try:
            result = await runner._deliver_completion_notification(format_process_notification(event), event)
        except BaseException:
            for waiting in [*retry, *events[index:]]:
                target_queue.put(waiting)
            raise
        if result is False:
            retry.append(event)
    return retry
