"""Thin REST-to-native glue: no queue, registry import, persistence or wire logging.

Only the already-loaded dashboard supervisor may dispatch. The core ``call``
entry point must itself require its registered server; a bare module import is
not authority. Hosted dashboard mounts intercept these routes in the parent;
calling from a plugin/compute child directly still fails closed, never creating
a second queue.
"""
import os
import sys

OPERATIONS = ('pending', 'respond', 'action-status')


class HumanInputUnavailable(RuntimeError):
    """The canonical supervisor cannot be reached in this process."""


def call(operation, body, *, principal):
    """Forward unchanged scope/result with the authenticated owner out of band.

    The HTTP caller checks the mounted profile first; the canonical service owns
    identity, epoch/revision, deadline, offered result and settlement validation.
    Never import server/methods here: an isolated plugin would get empty globals.
    """
    if operation not in OPERATIONS or not isinstance(body, dict) or not isinstance(principal, str) or not principal:
        raise ValueError('Invalid human input request')
    if (os.environ.get('HERMES_PLUGIN_HOST_PROCESS') == '1' or
            os.environ.get('HERMES_COMPUTE_HOST_CHILD') == '1'):
        raise HumanInputUnavailable('Human input unavailable')
    server = sys.modules.get('tui_gateway.server')
    methods = sys.modules.get('tui_gateway.methods_human_input')
    dispatch = getattr(methods, 'call', None)
    if server is None or not callable(dispatch):
        raise HumanInputUnavailable('Human input unavailable')
    result = dispatch(operation, body, principal=principal)
    if not isinstance(result, dict):
        raise HumanInputUnavailable('Human input unavailable')
    return result
