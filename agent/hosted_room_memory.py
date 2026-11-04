"""Ephemeral server attestation for a single canonical hosted member admission.

Not a wire field, prompt marker or session property. Providers read the current
binding at write time; cached agents and delegated sessions cannot reuse it.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_current: ContextVar[tuple | None] = ContextVar('hosted_room_memory', default=None)


@contextmanager
def hosted_room_memory_scope(session_id, profile_id, context):
    active = [True]  # shared with copied Contexts: reset alone cannot revoke background copies
    token = _current.set((session_id, profile_id, deepcopy(context), active))
    try:
        yield
    finally:
        active[0] = False
        _current.reset(token)


def current_hosted_room_memory(session_id, profile_id):
    bound = _current.get()
    if bound is None or bound[:2] != (session_id, profile_id) or not bound[3][0]:
        return None
    return deepcopy(bound[2])
