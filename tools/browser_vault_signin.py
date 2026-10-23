"""Owner-selected saved-login requests and exact supervised-page binding."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
import threading
import time
import uuid
from typing import Any


@dataclass(frozen=True)
class SigninTarget:
    task_id: str
    target_id: str
    origin: str
    supervisor: Any


fill_target: ContextVar[SigninTarget | None] = ContextVar('desktop_signin_target', default=None)


def bound_supervisor(task_id: str):
    binding = fill_target.get()
    return binding.supervisor if binding is not None and binding.task_id == task_id else None


def bound_target_matches(supervisor, task_id: str) -> bool:
    binding = fill_target.get()
    return binding is None or (binding.task_id == task_id and binding.supervisor is supervisor and
                               binding.target_id == getattr(supervisor, 'target_id', None))


def _offered_login(origin: str):
    from agent.vault_backends import enabled_backends

    for backend in enabled_backends():
        if backend.needs_unlock and not backend.is_unlocked():
            continue
        for item in backend.list_items():
            allowed = list(item.allowed_origins) or ([item.origin] if item.origin else [])
            if item.kind == 'login' and not item.any_origin and origin in allowed:
                identifier = item.identifier or ''
                masked = identifier[:1] + '•••' + ('@' + identifier.rsplit('@', 1)[1] if '@' in identifier else '') if identifier else None
                return item.id, masked
    return None, None


def request_signin(reason: str, task_id: str = 'default', *, timeout_s: float = 300) -> str:
    from tools import browser_vault_tool as vault
    from tools.desktop_admission import is_desktop_cdp, task_endpoint, assert_agent_admitted
    from tools.desktop_spaces import login_origin, producer_context

    def refuse(kind):
        return json.dumps({'success': False, 'error_type': kind})

    endpoint = task_endpoint(task_id)
    if not is_desktop_cdp(endpoint):
        return refuse('desktop_required')
    assert_agent_admitted(endpoint)
    try:
        origin = login_origin(vault._current_page_origin(task_id) or '')
    except ValueError:
        return refuse('origin_denied')
    if not reason or len(reason) > 512 or not 0 < timeout_s <= 300:
        return refuse('invalid_request')
    from tools.browser_vault_signin_cdp import published_target
    try:
        target_id = published_target(task_id, origin)
    except (RuntimeError, ValueError, KeyError):
        return refuse('supervisor_required')
    handle, masked = _offered_login(origin)
    store, scope = producer_context()
    request_id, expires_at = uuid.uuid4().hex, time.time() + timeout_s
    store.publish_signin('desktop', scope, request_id=request_id, revision=1, origin=origin,
                         reason=reason, expires_at=expires_at, login_handle=handle,
                         masked_identifier=masked, task_id=task_id, target_id=target_id)
    waiter = threading.Event()
    while True:
        outcome = store.signin_outcome('desktop', scope, request_id)
        if outcome != 'pending':
            return json.dumps({'success': outcome in ('filled', 'human_done'), 'status': outcome,
                               'request_id': request_id})
        waiter.wait(min(0.25, max(0, expires_at - time.time())))


def fill_requested_login(binding: dict, origin: str) -> bool:
    from tools import browser_vault_tool as vault
    from agent.vault_backends import backend_for_handle

    task_id = binding['task_id']
    backend = backend_for_handle(binding['login_handle'])
    meta = backend.get_meta(binding['login_handle']) if backend is not None else None
    if meta is None or meta.kind != 'login' or meta.any_origin:
        return False
    allowed = list(meta.allowed_origins) or ([meta.origin] if meta.origin else [])
    if origin not in allowed:
        return False
    from tools.browser_vault_signin_cdp import signin_page
    try:
        with signin_page(binding['target_id'], origin) as supervisor:
            token = fill_target.set(SigninTarget(task_id, binding['target_id'], origin, supervisor))
            try:
                if vault._current_page_origin(task_id) != origin:
                    return False
                result = json.loads(vault.browser_vault_fill(binding['login_handle'], task_id=task_id))
                return result.get('success') is True
            finally:
                fill_target.reset(token)
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return False
