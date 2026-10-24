"""A stand-in for Hermes's ``gateway.session_context`` for checkouts without Hermes.

It mirrors the parts the command relies on, including the host's behaviour that
``get_session_env`` falls back to ``os.environ`` when a ContextVar was never
bound. That fallback is the trap the command must avoid, so the stand-in has to
reproduce it for the tests to mean anything.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any

_UNSET: Any = object()
_NAMES = (
    "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE", "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE", "HERMES_SESSION_THREAD_ID", "HERMES_SESSION_USER_ID",
)
_VAR_MAP = {name: ContextVar(name, default=_UNSET) for name in _NAMES}
_FIELDS = {
    "platform": "HERMES_SESSION_PLATFORM", "source": "HERMES_SESSION_SOURCE",
    "chat_id": "HERMES_SESSION_CHAT_ID", "chat_type": "HERMES_SESSION_CHAT_TYPE",
    "thread_id": "HERMES_SESSION_THREAD_ID", "user_id": "HERMES_SESSION_USER_ID",
}


def set_session_vars(**values: str) -> list:
    return [_VAR_MAP[_FIELDS[key]].set(values.get(key, "")) for key in _FIELDS]


def clear_session_vars(tokens: list) -> None:
    for var in _VAR_MAP.values():
        var.set("")


def reset_session_vars() -> None:
    for var in _VAR_MAP.values():
        var.set(_UNSET)


def get_session_env(name: str, default: str = "") -> str:
    var = _VAR_MAP.get(name)
    if var is not None and (value := var.get()) is not _UNSET:
        return value
    return os.getenv(name, default)
