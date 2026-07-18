"""Runtime-only provenance capabilities for external-memory writes.

The model, tool arguments, restored transcripts, and wire payloads may all carry
strings that *claim* to be user-authored.  None of those strings are authority.
This module mints sealed in-process receipts only at authenticated runtime
boundaries and binds them to one concrete turn without retaining message text.

The types are generic to external memory providers.  Provider-specific policy
(for example which authenticated principal maps to a canonical peer) remains in
the provider.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Optional

_SCHEMA_REVISION = "hermes-memory-provenance/v1"
_CAPABILITY = object()
_HMAC_KEY = secrets.token_bytes(32)
_ALLOWED_RUNTIME_CLASSES = frozenset({"gateway", "cli", "api"})
_ALLOWED_ORIGIN_CLASSES = frozenset(
    {
        "authenticated_human_gateway",
        "authenticated_human_cli",
        "authenticated_human_api",
    }
)
_DEFAULT_POLICY_REVISION = "memory-source-policy/v1"
_DEFAULT_WRITER_RELEASE = "hermes-memory-boundary/v1"
_DEFAULT_MAX_AGE_SECONDS = 15 * 60.0
_MAX_REPLAY_ENTRIES = 8192

_CURRENT_ORIGIN: ContextVar[Optional["MemoryOriginReceipt"]] = ContextVar(
    "HERMES_MEMORY_ORIGIN_RECEIPT", default=None
)
_REPLAY_LOCK = threading.Lock()
_CONSUMED_NONCES: "OrderedDict[str, float]" = OrderedDict()


def _digest(*parts: str) -> str:
    payload = "\x1f".join(str(part or "") for part in parts).encode("utf-8", "surrogatepass")
    return hmac.new(_HMAC_KEY, payload, hashlib.sha256).hexdigest()


def _content_digest(content: Any) -> str:
    if content is None:
        normalized = ""
    elif isinstance(content, str):
        normalized = content
    else:
        normalized = str(content)
    return _digest("content", normalized)


def _principal_digest(platform: str, principal_id: str) -> str:
    return _digest("principal", platform.strip().lower(), principal_id.strip())


@dataclass(frozen=True, slots=True)
class MemoryOriginReceipt:
    """Sealed proof that a runtime boundary authenticated a human principal."""

    schema_revision: str
    runtime_class: str
    origin_class: str
    platform: str
    profile: str
    principal_hmac: str
    principal_alias: str
    adapter_auth_receipt_id: str
    source_observation_id: str
    policy_revision: str
    writer_release: str
    human_authored: bool
    synthetic: bool
    issued_at: float
    nonce: str
    _seal: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class MemoryTurnEnvelope:
    """Content-free, single-turn external-memory provenance capability."""

    schema_revision: str
    session_id: str
    turn_id: str
    message_id: str
    runtime_class: str
    origin_class: str
    platform: str
    profile: str
    principal_hmac: str
    principal_alias: str
    adapter_auth_receipt_id: str
    source_observation_id: str
    policy_revision: str
    writer_release: str
    human_authored: bool
    synthetic: bool
    content_hmac: str
    issued_at: float
    nonce: str
    _seal: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class MemoryWriteReceipt:
    """Proof that one built-in memory-tool mutation committed in a trusted turn."""

    schema_revision: str
    session_id: str
    turn_id: str
    turn_binding_hmac: str
    runtime_class: str
    origin_class: str
    platform: str
    profile: str
    principal_hmac: str
    principal_alias: str
    source_observation_id: str
    tool_call_id: str
    action: str
    target: str
    content_hmac: str
    policy_revision: str
    writer_release: str
    issued_at: float
    nonce: str
    _seal: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class MemoryToolReceipt:
    """Proof that the runtime dispatched one concrete external-memory tool call."""

    schema_revision: str
    session_id: str
    turn_id: str
    turn_binding_hmac: str
    runtime_class: str
    origin_class: str
    platform: str
    profile: str
    principal_hmac: str
    principal_alias: str
    source_observation_id: str
    tool_name: str
    tool_call_id: str
    policy_revision: str
    writer_release: str
    issued_at: float
    nonce: str
    _seal: object = field(repr=False, compare=False)


def _valid_origin(receipt: Any) -> bool:
    return (
        type(receipt) is MemoryOriginReceipt
        and receipt._seal is _CAPABILITY
        and receipt.schema_revision == _SCHEMA_REVISION
        and receipt.runtime_class in _ALLOWED_RUNTIME_CLASSES
        and receipt.origin_class in _ALLOWED_ORIGIN_CLASSES
        and receipt.human_authored is True
        and receipt.synthetic is False
        and bool(receipt.principal_hmac or receipt.principal_alias)
        and bool(receipt.policy_revision)
        and bool(receipt.writer_release)
    )


def issue_authenticated_origin(
    *,
    runtime_class: str,
    origin_class: str,
    platform: str,
    profile: str,
    principal_id: str,
    principal_alias: str = "",
    adapter_receipt_id: str,
    source_observation_id: str,
    policy_revision: str = _DEFAULT_POLICY_REVISION,
    writer_release: str = _DEFAULT_WRITER_RELEASE,
    issued_at: Optional[float] = None,
) -> MemoryOriginReceipt:
    """Mint an authenticated origin receipt at a trusted runtime boundary."""

    runtime_class = str(runtime_class or "").strip().lower()
    origin_class = str(origin_class or "").strip().lower()
    platform = str(platform or "").strip().lower()
    principal_id = str(principal_id or "").strip()
    if runtime_class not in _ALLOWED_RUNTIME_CLASSES:
        raise ValueError("unsupported authenticated memory runtime class")
    if origin_class not in _ALLOWED_ORIGIN_CLASSES:
        raise ValueError("unsupported authenticated memory origin class")
    if not platform or not principal_id or not adapter_receipt_id or not source_observation_id:
        raise ValueError("authenticated memory origin requires principal and auth receipts")
    return MemoryOriginReceipt(
        schema_revision=_SCHEMA_REVISION,
        runtime_class=runtime_class,
        origin_class=origin_class,
        platform=platform,
        profile=str(profile or "").strip(),
        principal_hmac=_principal_digest(platform, principal_id),
        principal_alias=str(principal_alias or "").strip(),
        adapter_auth_receipt_id=str(adapter_receipt_id),
        source_observation_id=str(source_observation_id),
        policy_revision=str(policy_revision or _DEFAULT_POLICY_REVISION),
        writer_release=str(writer_release or _DEFAULT_WRITER_RELEASE),
        human_authored=True,
        synthetic=False,
        issued_at=float(time.monotonic() if issued_at is None else issued_at),
        nonce=secrets.token_hex(16),
        _seal=_CAPABILITY,
    )


def issue_deny_origin(
    *,
    runtime_class: str = "unknown",
    origin_class: str = "unknown",
    platform: str = "",
    profile: str = "",
    policy_revision: str = _DEFAULT_POLICY_REVISION,
    writer_release: str = _DEFAULT_WRITER_RELEASE,
    issued_at: Optional[float] = None,
) -> MemoryOriginReceipt:
    """Mint an explicit no-write origin for unknown or synthetic paths."""

    return MemoryOriginReceipt(
        schema_revision=_SCHEMA_REVISION,
        runtime_class=str(runtime_class or "unknown").strip().lower(),
        origin_class=str(origin_class or "unknown").strip().lower(),
        platform=str(platform or "").strip().lower(),
        profile=str(profile or "").strip(),
        principal_hmac="",
        principal_alias="",
        adapter_auth_receipt_id="",
        source_observation_id="",
        policy_revision=str(policy_revision or _DEFAULT_POLICY_REVISION),
        writer_release=str(writer_release or _DEFAULT_WRITER_RELEASE),
        human_authored=False,
        synthetic=True,
        issued_at=float(time.monotonic() if issued_at is None else issued_at),
        nonce=secrets.token_hex(16),
        _seal=_CAPABILITY,
    )


def bind_memory_origin(receipt: MemoryOriginReceipt) -> Token:
    """Bind a sealed origin receipt to the current task/thread context."""

    if type(receipt) is not MemoryOriginReceipt or receipt._seal is not _CAPABILITY:
        receipt = issue_deny_origin(origin_class="invalid_origin_object")
    return _CURRENT_ORIGIN.set(receipt)


def clear_memory_origin(token: Optional[Token] = None) -> None:
    """Clear or reset the current task-local memory origin."""

    if token is not None:
        try:
            _CURRENT_ORIGIN.reset(token)
            return
        except (LookupError, RuntimeError, ValueError):
            pass
    _CURRENT_ORIGIN.set(None)


def current_memory_origin() -> Optional[MemoryOriginReceipt]:
    return _CURRENT_ORIGIN.get()


@contextmanager
def authenticated_local_cli_origin(*, profile: str = "default"):
    """Bind one local human CLI invocation; autonomous CLI loops do not use it."""
    import os

    receipt = issue_authenticated_origin(
        runtime_class="cli",
        origin_class="authenticated_human_cli",
        platform="cli",
        profile=profile or "default",
        principal_id=f"local-uid:{os.getuid()}",
        principal_alias="local_owner",
        adapter_receipt_id=secrets.token_hex(16),
        source_observation_id=secrets.token_hex(16),
    )
    token = bind_memory_origin(receipt)
    try:
        yield receipt
    finally:
        clear_memory_origin(token)


def issue_turn_envelope(
    origin: Optional[MemoryOriginReceipt],
    *,
    session_id: str,
    turn_id: str,
    message_id: str,
    user_content: Any,
    issued_at: Optional[float] = None,
) -> MemoryTurnEnvelope:
    """Bind the current authenticated origin to one concrete user turn."""

    receipt = origin if origin is not None else current_memory_origin()
    if type(receipt) is not MemoryOriginReceipt or receipt._seal is not _CAPABILITY:
        receipt = issue_deny_origin()
    return MemoryTurnEnvelope(
        schema_revision=_SCHEMA_REVISION,
        session_id=str(session_id or ""),
        turn_id=str(turn_id or ""),
        message_id=str(message_id or ""),
        runtime_class=receipt.runtime_class,
        origin_class=receipt.origin_class,
        platform=receipt.platform,
        profile=receipt.profile,
        principal_hmac=receipt.principal_hmac,
        principal_alias=receipt.principal_alias,
        adapter_auth_receipt_id=receipt.adapter_auth_receipt_id,
        source_observation_id=receipt.source_observation_id,
        policy_revision=receipt.policy_revision,
        writer_release=receipt.writer_release,
        human_authored=receipt.human_authored,
        synthetic=receipt.synthetic,
        content_hmac=_content_digest(user_content),
        issued_at=float(time.monotonic() if issued_at is None else issued_at),
        nonce=secrets.token_hex(16),
        _seal=_CAPABILITY,
    )


def principal_matches(
    envelope: Any,
    *,
    platform: str,
    principal_id: str,
) -> bool:
    """Compare a private configured principal to an opaque envelope HMAC."""

    if type(envelope) not in {MemoryTurnEnvelope, MemoryWriteReceipt, MemoryToolReceipt}:
        return False
    if envelope._seal is not _CAPABILITY:
        return False
    principal_id = str(principal_id or "").strip()
    if not principal_id:
        return False
    candidate = _principal_digest(str(platform or ""), principal_id)
    return hmac.compare_digest(envelope.principal_hmac, candidate)


def _consume_nonce(nonce: str, issued_at: float) -> bool:
    with _REPLAY_LOCK:
        if nonce in _CONSUMED_NONCES:
            return False
        _CONSUMED_NONCES[nonce] = issued_at
        _CONSUMED_NONCES.move_to_end(nonce)
        while len(_CONSUMED_NONCES) > _MAX_REPLAY_ENTRIES:
            _CONSUMED_NONCES.popitem(last=False)
        return True


def _sealed_eligible_turn(envelope: Any) -> bool:
    return (
        type(envelope) is MemoryTurnEnvelope
        and envelope._seal is _CAPABILITY
        and envelope.schema_revision == _SCHEMA_REVISION
        and envelope.runtime_class in _ALLOWED_RUNTIME_CLASSES
        and envelope.origin_class in _ALLOWED_ORIGIN_CLASSES
        and envelope.human_authored is True
        and envelope.synthetic is False
        and bool(envelope.session_id)
        and bool(envelope.turn_id)
        and bool(envelope.message_id)
        and bool(envelope.policy_revision)
        and bool(envelope.writer_release)
    )


def issue_memory_tool_receipt(
    turn_envelope: Any,
    *,
    tool_name: str,
    tool_call_id: str,
    issued_at: Optional[float] = None,
) -> Optional[MemoryToolReceipt]:
    """Mint a content-free capability for one executor-issued memory tool call."""

    tool_name = str(tool_name or "").strip()
    tool_call_id = str(tool_call_id or "").strip()
    if not _sealed_eligible_turn(turn_envelope) or not tool_name or not tool_call_id:
        return None
    return MemoryToolReceipt(
        schema_revision=_SCHEMA_REVISION,
        session_id=turn_envelope.session_id,
        turn_id=turn_envelope.turn_id,
        turn_binding_hmac=_digest(
            "turn-binding",
            turn_envelope.nonce,
            turn_envelope.content_hmac,
            turn_envelope.principal_hmac,
        ),
        runtime_class=turn_envelope.runtime_class,
        origin_class=turn_envelope.origin_class,
        platform=turn_envelope.platform,
        profile=turn_envelope.profile,
        principal_hmac=turn_envelope.principal_hmac,
        principal_alias=turn_envelope.principal_alias,
        source_observation_id=turn_envelope.source_observation_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        policy_revision=turn_envelope.policy_revision,
        writer_release=turn_envelope.writer_release,
        issued_at=float(time.monotonic() if issued_at is None else issued_at),
        nonce=secrets.token_hex(16),
        _seal=_CAPABILITY,
    )


def validate_memory_tool_receipt(
    receipt: Any,
    *,
    tool_name: str,
    tool_call_id: str,
    turn_envelope: Any = None,
    consume: bool = False,
    now: Optional[float] = None,
    max_age_seconds: float = _DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    """Validate an executor-issued tool capability, optionally consuming it once."""

    if type(receipt) is not MemoryToolReceipt or receipt._seal is not _CAPABILITY:
        return False
    if receipt.schema_revision != _SCHEMA_REVISION:
        return False
    if receipt.runtime_class not in _ALLOWED_RUNTIME_CLASSES:
        return False
    if receipt.origin_class not in _ALLOWED_ORIGIN_CLASSES:
        return False
    if not receipt.principal_hmac and not receipt.principal_alias:
        return False
    if receipt.tool_name != str(tool_name or "").strip():
        return False
    if receipt.tool_call_id != str(tool_call_id or "").strip():
        return False
    if turn_envelope is not None:
        if not _sealed_eligible_turn(turn_envelope):
            return False
        expected_binding = _digest(
            "turn-binding",
            turn_envelope.nonce,
            turn_envelope.content_hmac,
            turn_envelope.principal_hmac,
        )
        if not hmac.compare_digest(receipt.turn_binding_hmac, expected_binding):
            return False
        if receipt.session_id != turn_envelope.session_id or receipt.turn_id != turn_envelope.turn_id:
            return False
        if receipt.principal_hmac != turn_envelope.principal_hmac:
            return False
        if receipt.policy_revision != turn_envelope.policy_revision:
            return False
        if receipt.writer_release != turn_envelope.writer_release:
            return False
    if not receipt.policy_revision or not receipt.writer_release:
        return False
    current = float(time.monotonic() if now is None else now)
    age = current - receipt.issued_at
    if age < -1.0 or age > max(0.0, float(max_age_seconds)):
        return False
    if consume and not _consume_nonce(receipt.nonce, receipt.issued_at):
        return False
    return True


def issue_memory_write_receipt(
    turn_envelope: Any,
    *,
    tool_call_id: str,
    action: str,
    target: str,
    content: Any,
    committed: bool,
    issued_at: Optional[float] = None,
) -> Optional[MemoryWriteReceipt]:
    """Mint a write capability only after a trusted built-in mutation commits."""

    tool_call_id = str(tool_call_id or "").strip()
    action = str(action or "").strip().lower()
    target = str(target or "").strip().lower()
    if committed is not True or not _sealed_eligible_turn(turn_envelope):
        return None
    if not tool_call_id or action not in {"add", "replace", "remove"}:
        return None
    if target not in {"memory", "user"}:
        return None
    return MemoryWriteReceipt(
        schema_revision=_SCHEMA_REVISION,
        session_id=turn_envelope.session_id,
        turn_id=turn_envelope.turn_id,
        turn_binding_hmac=_digest(
            "turn-binding",
            turn_envelope.nonce,
            turn_envelope.content_hmac,
            turn_envelope.principal_hmac,
        ),
        runtime_class=turn_envelope.runtime_class,
        origin_class=turn_envelope.origin_class,
        platform=turn_envelope.platform,
        profile=turn_envelope.profile,
        principal_hmac=turn_envelope.principal_hmac,
        principal_alias=turn_envelope.principal_alias,
        source_observation_id=turn_envelope.source_observation_id,
        tool_call_id=tool_call_id,
        action=action,
        target=target,
        content_hmac=_content_digest(content),
        policy_revision=turn_envelope.policy_revision,
        writer_release=turn_envelope.writer_release,
        issued_at=float(time.monotonic() if issued_at is None else issued_at),
        nonce=secrets.token_hex(16),
        _seal=_CAPABILITY,
    )


def validate_memory_write_receipt(
    receipt: Any,
    *,
    turn_envelope: Any = None,
    tool_call_id: str,
    action: str,
    target: str,
    content: Any,
    consume: bool = False,
    now: Optional[float] = None,
    max_age_seconds: float = _DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    """Validate a committed-write capability, optionally consuming it once."""

    if type(receipt) is not MemoryWriteReceipt or receipt._seal is not _CAPABILITY:
        return False
    if receipt.schema_revision != _SCHEMA_REVISION:
        return False
    if receipt.runtime_class not in _ALLOWED_RUNTIME_CLASSES:
        return False
    if receipt.origin_class not in _ALLOWED_ORIGIN_CLASSES:
        return False
    if not receipt.principal_hmac and not receipt.principal_alias:
        return False
    if turn_envelope is not None:
        if not _sealed_eligible_turn(turn_envelope):
            return False
        expected_binding = _digest(
            "turn-binding",
            turn_envelope.nonce,
            turn_envelope.content_hmac,
            turn_envelope.principal_hmac,
        )
        if not hmac.compare_digest(receipt.turn_binding_hmac, expected_binding):
            return False
        if receipt.session_id != turn_envelope.session_id or receipt.turn_id != turn_envelope.turn_id:
            return False
        if receipt.principal_hmac != turn_envelope.principal_hmac:
            return False
        if receipt.policy_revision != turn_envelope.policy_revision:
            return False
        if receipt.writer_release != turn_envelope.writer_release:
            return False
    if receipt.tool_call_id != str(tool_call_id or ""):
        return False
    if receipt.action != str(action or "").strip().lower():
        return False
    if receipt.target != str(target or "").strip().lower():
        return False
    if not hmac.compare_digest(receipt.content_hmac, _content_digest(content)):
        return False
    if not receipt.policy_revision or not receipt.writer_release:
        return False
    current = float(time.monotonic() if now is None else now)
    age = current - receipt.issued_at
    if age < -1.0 or age > max(0.0, float(max_age_seconds)):
        return False
    if consume and not _consume_nonce(receipt.nonce, receipt.issued_at):
        return False
    return True


def validate_turn_envelope(
    envelope: Any,
    *,
    user_content: Any,
    session_id: str,
    turn_id: str = "",
    consume: bool = False,
    now: Optional[float] = None,
    max_age_seconds: float = _DEFAULT_MAX_AGE_SECONDS,
    expected_policy_revision: str = "",
    expected_writer_release: str = "",
) -> bool:
    """Validate a turn capability, optionally consuming it exactly once."""

    if type(envelope) is not MemoryTurnEnvelope or envelope._seal is not _CAPABILITY:
        return False
    if envelope.schema_revision != _SCHEMA_REVISION:
        return False
    if envelope.runtime_class not in _ALLOWED_RUNTIME_CLASSES:
        return False
    if envelope.origin_class not in _ALLOWED_ORIGIN_CLASSES:
        return False
    if envelope.human_authored is not True or envelope.synthetic is not False:
        return False
    if not envelope.session_id or envelope.session_id != str(session_id or ""):
        return False
    if turn_id and envelope.turn_id != str(turn_id):
        return False
    if not envelope.turn_id or not envelope.message_id:
        return False
    if not hmac.compare_digest(envelope.content_hmac, _content_digest(user_content)):
        return False
    current = float(time.monotonic() if now is None else now)
    age = current - envelope.issued_at
    if age < -1.0 or age > max(0.0, float(max_age_seconds)):
        return False
    if expected_policy_revision and envelope.policy_revision != expected_policy_revision:
        return False
    if expected_writer_release and envelope.writer_release != expected_writer_release:
        return False
    if not envelope.policy_revision or not envelope.writer_release:
        return False
    if consume and not _consume_nonce(envelope.nonce, envelope.issued_at):
        return False
    return True
