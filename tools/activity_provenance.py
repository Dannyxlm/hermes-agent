"""Capture immutable display ownership while the spawning profile/parent still exists."""
from contextlib import contextmanager
from contextvars import Context
import threading

from hermes_constants import get_hermes_home

_delegate_calls_lock = threading.Lock()
_delegate_calls = {}


def worker_provenance(session_id: str, parent_task_id: str | None = None) -> dict[str, str]:
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock

    home = str(get_hermes_home().resolve())
    conversation = session_id if isinstance(session_id, str) else ''
    # An exact registered parent carries the conversation through nested delegates.
    # Capture now: the parent record may disappear before its child's process exits.
    if isinstance(parent_task_id, str) and parent_task_id:
        with _active_subagents_lock:
            parent = _active_subagents.get(parent_task_id)
            if parent is not None:
                conversation = (parent.get('owner_conversation_id') or ''
                                if parent.get('owner_profile_home') == home else '')
    return {'owner_profile_home': home, 'owner_conversation_id': conversation}


@contextmanager
def incomplete_delegate_call(parent_agent):
    """Construction and synchronous admission have no complete child roster yet."""
    owner = worker_provenance(getattr(parent_agent, 'session_id', ''),
                              getattr(parent_agent, '_subagent_id', None))
    token = object()
    with _delegate_calls_lock:
        _delegate_calls[token] = owner
    try:
        yield
    finally:
        with _delegate_calls_lock:
            _delegate_calls.pop(token, None)


def incomplete_subagent_scopes():
    """Conservative completeness fences, never invented queued worker identities.

    Active async units retain the fence after delegate_task returns. Their captured
    Context resolves the served or launch profile through the normal home resolver;
    a legacy record without that Context cannot certify any profile empty. The
    existing async registry owns cancellation/failure cleanup.
    """
    from tools.async_delegation import _records, _records_lock, _LIVE_STATES

    with _delegate_calls_lock:
        owners = [dict(owner) for owner in _delegate_calls.values()]
    with _records_lock:
        pending = [(row.get('_context'), row.get('parent_session_id'))
                   for row in _records.values() if row.get('status') in _LIVE_STATES]
    for context, conversation in pending:
        home = context.copy().run(lambda: str(get_hermes_home().resolve())) if isinstance(context, Context) else ''
        owners.append({'owner_profile_home': home,
                       'owner_conversation_id': conversation})
    return [{**owner, '_snapshot_incomplete': True} for owner in owners]
