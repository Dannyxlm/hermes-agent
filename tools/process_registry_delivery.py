"""Notification disposition stored in the producer's existing completed-result receipt.

``pending`` proves no admission was attempted. ``unknown`` is written before crossing
that boundary: only an exact FIFO receipt can settle it, never age or a missing ACK.
Old result files have no such proof and are deliberately not replayed.
"""
import json
import re
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_cli.active_sessions import _FileLock
from utils import atomic_json_write


UNSETTLED = frozenset({'pending', 'unknown'})


def uncertain_legacy_notification(state):
    """No durable FIFO was involved, so there is nothing a later scan can reconcile."""
    return state.get('status') == 'unknown' and state.get('reason') in {
        'legacy_consumer_handoff', 'volatile_delivery_after_persistence_failure'}


def result_path(session_id):
    if not re.fullmatch(r'proc_[\w]+', session_id):
        raise ValueError('invalid process result id')
    return get_hermes_home() / 'logs' / 'process-results' / f'{session_id}.json'


def completion_event(session, *, durable=False):
    from tools.process_registry import _completion_output, _redact_process_result
    from tools.process_launches import fields
    event = {
        'type': 'completion', 'session_id': session.id, 'session_key': session.session_key,
        '_process_result_route': session.session_key,
        'parent_session_id': session.parent_session_id, '_process_result_parent': session.parent_session_id,
        'task_id': session.task_id,
        'owner_task_id': session.owner_task_id, 'command': session.command,
        'started_at': session.started_at, 'exit_code': session.exit_code,
        'completion_reason': session.completion_reason, 'termination_source': session.termination_source,
        **fields(session), **_completion_output(session),
        **{key: getattr(session, 'watcher_' + key) for key in (
            'platform', 'chat_id', 'user_id', 'user_name', 'thread_id', 'message_id')},
        '_process_result_home': str(get_hermes_home().resolve()),
    }
    if session.handoff_note:
        event['handoff_note'] = session.handoff_note
    if session._result_persistence_error and not durable:
        event['_process_result_durability_error'] = session._result_persistence_error
    if durable:
        from agent.redact import redact_sensitive_text, redact_terminal_output
        event["output"] = redact_terminal_output(event["output"], event["command"], force=True)
        event["command"] = redact_sensitive_text(event["command"], code_file=True, force=True)
    else:
        _redact_process_result(event)
    return event


def new_notification(session):
    event = completion_event(session, durable=True)
    consumed = session._notification_consumed
    uncertain = bool(session._result_persistence_error) and not consumed
    return {'identity': ['completion', session.id, session.started_at],
            'status': 'terminal' if consumed else 'unknown' if uncertain else 'pending', 'event': event,
            'admission_id': None, 'reason': 'consumed' if consumed else (
                'volatile_delivery_after_persistence_failure' if uncertain else None)}


def _matches(notification, event):
    return (notification.get('identity') == ['completion', event.get('session_id'), event.get('started_at')]
            and notification.get('event', {}).get('parent_session_id') == event.get(
                '_process_result_parent', event.get('parent_session_id'))
            and notification.get('event', {}).get('session_key') == event.get(
                '_process_result_route', event.get('session_key')))


def notification_state(event):
    """Read only in the owning profile; an event from another store cannot settle this one."""
    if event.get('type') != 'completion':
        return None
    home = event.get('_process_result_home')
    if home and Path(home).resolve() != get_hermes_home().resolve():
        raise ValueError('process notification profile mismatch')
    if event.get('_process_result_durability_error'):
        return None  # Live-only fallback: do not pretend there is a valid producer receipt.
    try:
        record = json.loads(result_path(str(event.get('session_id') or '')).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, ValueError):
        if home:
            raise ValueError('process notification receipt unavailable') from None
        return None
    notification = record.get('notification')
    if notification and _matches(notification, event):
        return notification
    if home:
        raise ValueError('process notification receipt identity mismatch')
    return None


def settle_notification(event, status, *, admission_id=None, reason=None, expected_status=None):
    """Update an exact producer receipt, preserving both result and terminal dispositions."""
    if status not in {'pending', 'unknown', 'admitted', 'terminal'}:
        raise ValueError('invalid process notification disposition')
    if event.get('_process_result_home'):
        notification_state(event)  # Validate ownership even when this profile has no result directory.
    try:
        path = result_path(str(event.get('session_id') or ''))
    except ValueError:
        return False
    if not path.parent.exists():
        return False
    with _FileLock(path.parent.with_suffix('.lock')):
        notification = notification_state(event)
        if (notification is None or notification['status'] not in UNSETTLED
                or (expected_status is not None and notification['status'] != expected_status)):
            return False
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        record['notification'] = {**notification, 'status': status, 'admission_id': admission_id,
                                  'reason': reason}
        atomic_json_write(path, record, mode=0o600)
        return True


def admission_process_events(event):
    """The exact same-owner result receipts carried by one existing completion batch."""
    events = [event] if notification_state(event) is not None else []
    primary = (event.get('_process_result_route', event.get('session_key')),
               event.get('_process_result_parent', event.get('parent_session_id')))
    for raw in event.get('_automation_identities') or ():
        identity = json.loads(raw)
        if len(identity) != 3 or identity[0] != 'completion' or identity[1] == event.get('session_id'):
            continue
        try:
            record = json.loads(result_path(identity[1]).read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            continue  # Legacy siblings need no producer receipt.
        notification = record.get('notification') or {}
        sibling = notification.get('event') or {}
        if (notification.get('identity') == identity
                and (sibling.get('session_key'), sibling.get('parent_session_id')) == primary):
            events.append(sibling)
    return events


def legacy_notification_ready(event):
    """Volatile legacy consumers cannot reconcile an uncertain prior handoff."""
    state = notification_state(event)
    return state is None or state['status'] == 'pending'


def legacy_notification_handoff(event):
    if notification_state(event) is None:
        return True
    return settle_notification(event, 'unknown', reason='legacy_consumer_handoff', expected_status='pending')


def legacy_notification_submitted(event, accepted):
    """A positive volatile handoff is terminal, not a durable FIFO admission or consumption.

    An explicit refusal proves no turn was accepted and permits retry. An exception
    does not: the pre-handoff unknown must survive without blind replay.
    """
    if notification_state(event) is not None:
        settle_notification(event, 'terminal' if accepted else 'pending',
                            reason='legacy_submit_accepted' if accepted else 'legacy_submit_refused',
                            expected_status='unknown')


def mark_completion_consumed(registry, session):
    registry._completion_consumed.add(session.id)
    session._notification_consumed = True
    if session._result_persistence_error:
        return  # The live wait/log succeeded; there is no durable receipt to acknowledge.
    event = {'type': 'completion', 'session_id': session.id, 'started_at': session.started_at,
             'parent_session_id': session.parent_session_id, 'session_key': session.session_key}
    settle_notification(event, 'terminal', reason='consumed', expected_status='pending')


def restore_process_completions(target_queue):
    """Offer pending obligations and unknown receipt reconciliations, never old ambiguous history."""
    from tools.process_registry_results import _result_paths
    with target_queue.mutex:
        offered = {(event.get("_process_result_home"), "completion", event.get("session_id"),
                    event.get("started_at")) for event in target_queue.queue
                   if event.get("type") == "completion"}
    count = 0
    for path in _result_paths():
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            continue  # A now-settled history receipt was pruned by another writer.
        notification = record.get('notification') or {}
        if notification.get('status') not in UNSETTLED or uncertain_legacy_notification(notification):
            continue
        event = notification['event']
        identity = (str(get_hermes_home().resolve()), *notification['identity'])
        if identity in offered or not _matches(notification, event):
            continue
        target_queue.put({**event, 'restored': True, '_process_completion_recovery': True})
        offered.add(identity)
        count += 1
    return count
