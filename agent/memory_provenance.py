"""Provider-neutral, content-free memory turn provenance.

The public :class:`MemoryTurnProvenance` fields serialize exactly as the
vendored ``memory-provenance/v1`` contract.  Session, turn, and message
bindings are deliberately runtime-only: that richer envelope has no canonical
cross-process schema yet and must not be smuggled into the v1 record.

Authority enters through a sealed ``TrustedMemoryIngress`` minted by trusted
code *after* transport/user authentication.  A one-shot ContextVar is only a
bridge from that ingress seam into turn construction; provider and tool code
must receive the resulting provenance explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional


PROVENANCE_SCHEMA_VERSION = "memory-provenance/v1"
SUBJECT_BINDINGS_SCHEMA_VERSION = "hermes-memory-subject-bindings/v1"
PROXY_PROOF_SCHEMA_VERSION = "hermes-memory-proxy-proof/v1"
PROXY_REPLAY_SCHEMA_VERSION = "hermes-memory-proxy-replay/v1"

UNIDENTIFIED_SUBJECT = "unidentified"
LOCAL_ONLY = "local_only"
READ_CONTAINMENT = "tools_only_read_containment"

PROXY_PROOF_HEADER = "X-Hermes-Memory-Proof"
MAX_SUBJECT_BINDINGS_BYTES = 64 * 1024
MAX_SUBJECT_BINDINGS = 512
MAX_PROXY_KEY_BYTES = 4096
MAX_PROXY_PROOF_BYTES = 8192
MAX_PROXY_REPLAY_BYTES = 512 * 1024
MAX_PROXY_REPLAY_ENTRIES = 4096
MAX_PROXY_PROOF_LIFETIME_SECONDS = 60
MAX_PROXY_CLOCK_SKEW_SECONDS = 5

_ORIGINS = frozenset(
    {
        "telegram_private",
        "telegram_group",
        "photon_api",
        "desktop_websocket",
        "cli",
        "tui",
        "cron",
        "background",
        "webhook",
        "delegated",
    }
)
_DEPLOYMENT_MODES = frozenset({LOCAL_ONLY, READ_CONTAINMENT})
_SAFE_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SAFE_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_SAFE_CONFIG_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_SAFE_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Per-process sealing material. It is not an authentication credential and is
# never serialized; it makes hand-built/tampered Python objects fail validation.
_SEAL_KEY = secrets.token_bytes(32)
_INGRESS_CAPABILITY = object()
_TURN_CAPABILITY = object()

_CURRENT_INGRESS: ContextVar[Optional["TrustedMemoryIngress"]] = ContextVar(
    "hermes_memory_trusted_ingress", default=None
)
_REPLAY_THREAD_LOCK = threading.Lock()


class MemoryProvenanceError(ValueError):
    """A content-safe provenance/configuration error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MemoryProvenanceError("invalid_json", "value is not canonical JSON") from exc


def canonical_proxy_json_bytes(value: Any) -> bytes:
    """Return the exact request bytes used by the signed proxy transport."""

    return _canonical_json_bytes(value)


def canonical_proxy_json_text(value: Any) -> str:
    return canonical_proxy_json_bytes(value).decode("utf-8")


def _strict_json_object(raw: bytes, *, context: str, max_bytes: int) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise MemoryProvenanceError("invalid_json", f"{context} exceeds its byte bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemoryProvenanceError("invalid_json", f"{context} is not UTF-8 JSON") from exc

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise MemoryProvenanceError(
                    "duplicate_json_key", f"{context} contains a duplicate key"
                )
            value[key] = item
        return value

    def _constant(_value: str) -> Any:
        raise MemoryProvenanceError("invalid_json", f"{context} contains a non-JSON number")

    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except MemoryProvenanceError:
        raise
    except json.JSONDecodeError as exc:
        raise MemoryProvenanceError("invalid_json", f"{context} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise MemoryProvenanceError("invalid_json", f"{context} must be an object")
    return value


def _require_absolute_path(value: os.PathLike[str] | str | None, *, code: str) -> Path:
    if value is None or not str(value).strip():
        raise MemoryProvenanceError(code, "an explicit absolute path is required")
    path = Path(str(value))
    if not path.is_absolute():
        raise MemoryProvenanceError(code, "the configured path must be absolute")
    if ".." in path.parts:
        raise MemoryProvenanceError(code, "the configured path must not traverse parents")
    try:
        encoded = os.fsencode(path)
        encoded_name = os.fsencode(path.name)
    except (TypeError, UnicodeError) as exc:
        raise MemoryProvenanceError(code, "the configured path is invalid") from exc
    if len(encoded) > 4096 or not encoded_name or len(encoded_name) > 240:
        raise MemoryProvenanceError(code, "the configured path exceeds its bound")
    return path


def _validate_private_stat(metadata: os.stat_result, *, code: str) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise MemoryProvenanceError(code, "the configured path must be a regular file")
    if metadata.st_mode & 0o077:
        raise MemoryProvenanceError(code, "the configured file must be owner-private")
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and metadata.st_uid != geteuid():
        raise MemoryProvenanceError(code, "the configured file must be owned by this user")


def _read_private_regular_file(
    path_value: os.PathLike[str] | str | None,
    *,
    context: str,
    code: str,
    maximum_bytes: int,
) -> tuple[Path, bytes]:
    path = _require_absolute_path(path_value, code=code)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MemoryProvenanceError(code, f"{context} must be a readable non-symlink file") from exc
    try:
        metadata = os.fstat(descriptor)
        _validate_private_stat(metadata, code=code)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise MemoryProvenanceError(code, f"{context} exceeds its byte bound")
        return path, raw
    finally:
        os.close(descriptor)


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise MemoryProvenanceError("invalid_timestamp", "timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, code: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MemoryProvenanceError(code, "timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryProvenanceError(code, "timestamp is invalid") from exc
    return _utc(parsed)


def _keyed_digest(namespace: str, value: str) -> str:
    material = f"{namespace}\x00{value}".encode("utf-8", errors="strict")
    return hmac.new(_SEAL_KEY, material, hashlib.sha256).hexdigest()


def _seal_payload(kind: str, payload: Mapping[str, Any]) -> str:
    return hmac.new(
        _SEAL_KEY,
        kind.encode("ascii") + b"\x00" + _canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _safe_alias(value: Any, *, field_name: str, allow_unidentified: bool = True) -> str:
    alias = str(value or "").strip()
    if not alias or not _SAFE_ALIAS_RE.fullmatch(alias):
        raise MemoryProvenanceError("invalid_provenance", f"{field_name} is invalid")
    if not allow_unidentified and alias == UNIDENTIFIED_SUBJECT:
        raise MemoryProvenanceError("invalid_provenance", f"{field_name} is unidentified")
    return alias


def _safe_origin(value: Any) -> str:
    origin = str(value or "").strip()
    if origin not in _ORIGINS:
        raise MemoryProvenanceError("invalid_provenance", "origin is unsupported")
    return origin


@dataclass(frozen=True, slots=True)
class MemorySubjectBindings:
    """A strict private caller-to-subject map and its exact content digest."""

    content_digest: str
    _bindings: tuple[tuple[str, str, str], ...] = field(repr=False)

    def resolve(self, platform: str, principal_id: str) -> str:
        platform_value = str(platform or "").strip().lower()
        principal_value = str(principal_id or "").strip()
        if not platform_value or not principal_value:
            return UNIDENTIFIED_SUBJECT
        for bound_platform, bound_principal, subject_id in self._bindings:
            if bound_platform != platform_value:
                continue
            try:
                if hmac.compare_digest(bound_principal.encode(), principal_value.encode()):
                    return subject_id
            except (TypeError, UnicodeEncodeError):
                return UNIDENTIFIED_SUBJECT
        return UNIDENTIFIED_SUBJECT


def load_memory_subject_bindings(
    path: os.PathLike[str] | str | None = None,
) -> MemorySubjectBindings:
    """Load the explicit owner-private subject binding file.

    The returned digest is intended for the signed runtime's aggregate observed
    configuration binding.  Raw principals are excluded from repr/loggable
    values and never enter provenance records.
    """

    configured = path if path is not None else os.getenv("HERMES_MEMORY_SUBJECT_BINDINGS_FILE")
    _path, raw = _read_private_regular_file(
        configured,
        context="memory subject bindings",
        code="unsafe_subject_bindings_file",
        maximum_bytes=MAX_SUBJECT_BINDINGS_BYTES,
    )
    payload = _strict_json_object(
        raw, context="memory subject bindings", max_bytes=MAX_SUBJECT_BINDINGS_BYTES
    )
    if set(payload) != {"schema_version", "bindings"}:
        raise MemoryProvenanceError(
            "invalid_subject_bindings", "memory subject bindings fields are invalid"
        )
    if payload.get("schema_version") != SUBJECT_BINDINGS_SCHEMA_VERSION:
        raise MemoryProvenanceError(
            "invalid_subject_bindings", "memory subject bindings version is unsupported"
        )
    rows = payload.get("bindings")
    if not isinstance(rows, list) or len(rows) > MAX_SUBJECT_BINDINGS:
        raise MemoryProvenanceError(
            "invalid_subject_bindings", "memory subject bindings list is invalid"
        )
    seen: set[tuple[str, str]] = set()
    bindings: list[tuple[str, str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"platform", "principal_id", "subject_id"}:
            raise MemoryProvenanceError(
                "invalid_subject_bindings", "memory subject binding row is invalid"
            )
        platform = str(row.get("platform") or "").strip().lower()
        principal = str(row.get("principal_id") or "").strip()
        subject = _safe_alias(row.get("subject_id"), field_name="subject_id", allow_unidentified=False)
        if not _SAFE_PLATFORM_RE.fullmatch(platform) or not principal or len(principal) > 512:
            raise MemoryProvenanceError(
                "invalid_subject_bindings", "memory subject binding row is invalid"
            )
        pair = (platform, principal)
        if pair in seen:
            raise MemoryProvenanceError(
                "duplicate_subject_binding", "duplicate platform/principal binding"
            )
        seen.add(pair)
        bindings.append((platform, principal, subject))
    return MemorySubjectBindings(
        content_digest=hashlib.sha256(raw).hexdigest(),
        _bindings=tuple(bindings),
    )


@dataclass(frozen=True, slots=True, init=False)
class TrustedMemoryIngress:
    """Sealed, content-free result of a trusted ingress authentication check."""

    caller_id: str
    subject_id: str
    origin: str
    authenticated_at: str
    deployment_mode: str
    _platform: str = field(repr=False)
    _profile: str = field(repr=False)
    _authenticated: bool = field(repr=False)
    _reason_code: str = field(repr=False)
    _seal: str = field(repr=False)

    def __init__(
        self,
        capability: object,
        *,
        caller_id: str,
        subject_id: str,
        origin: str,
        authenticated_at: str,
        deployment_mode: str,
        platform: str,
        profile: str,
        authenticated: bool,
        reason_code: str,
        seal: str,
    ) -> None:
        if capability is not _INGRESS_CAPABILITY:
            raise MemoryProvenanceError("untrusted_ingress", "ingress must be minted by a trusted boundary")
        for name, value in (
            ("caller_id", caller_id),
            ("subject_id", subject_id),
            ("origin", origin),
            ("authenticated_at", authenticated_at),
            ("deployment_mode", deployment_mode),
            ("_platform", platform),
            ("_profile", profile),
            ("_authenticated", bool(authenticated)),
            ("_reason_code", reason_code),
            ("_seal", seal),
        ):
            object.__setattr__(self, name, value)

    @property
    def authenticated(self) -> bool:
        return self._authenticated


def _ingress_payload(
    *,
    caller_id: str,
    subject_id: str,
    origin: str,
    authenticated_at: str,
    deployment_mode: str,
    platform: str,
    profile: str,
    authenticated: bool,
    reason_code: str,
) -> dict[str, Any]:
    return {
        "caller_id": caller_id,
        "subject_id": subject_id,
        "origin": origin,
        "authenticated_at": authenticated_at,
        "deployment_mode": deployment_mode,
        "platform": platform,
        "profile": profile,
        "authenticated": authenticated,
        "reason_code": reason_code,
    }


def _new_ingress(**values: Any) -> TrustedMemoryIngress:
    payload = _ingress_payload(**values)
    return TrustedMemoryIngress(
        _INGRESS_CAPABILITY,
        **values,
        seal=_seal_payload("ingress", payload),
    )


def _valid_ingress(value: Any) -> bool:
    if not isinstance(value, TrustedMemoryIngress):
        return False
    try:
        payload = _ingress_payload(
            caller_id=_safe_alias(value.caller_id, field_name="caller_id"),
            subject_id=_safe_alias(value.subject_id, field_name="subject_id"),
            origin=_safe_origin(value.origin),
            authenticated_at=_format_timestamp(_parse_timestamp(value.authenticated_at, code="invalid_provenance")),
            deployment_mode=value.deployment_mode,
            platform=value._platform,
            profile=value._profile,
            authenticated=value._authenticated,
            reason_code=value._reason_code,
        )
        if payload["deployment_mode"] not in _DEPLOYMENT_MODES:
            return False
        expected = _seal_payload("ingress", payload)
        return hmac.compare_digest(expected, value._seal)
    except Exception:
        return False


def issue_authenticated_ingress(
    *,
    origin: str,
    platform: str,
    principal_id: str,
    subject_id: str = UNIDENTIFIED_SUBJECT,
    profile: str = "default",
    deployment_mode: str | None = None,
    authenticated_at: datetime | None = None,
) -> TrustedMemoryIngress:
    """Mint a trusted ingress only after the caller authenticated externally."""

    principal = str(principal_id or "").strip()
    if not principal or len(principal) > 512:
        raise MemoryProvenanceError("invalid_principal", "authenticated principal is missing")
    origin_value = _safe_origin(origin)
    subject = _safe_alias(subject_id, field_name="subject_id")
    mode = deployment_mode or (
        READ_CONTAINMENT if subject != UNIDENTIFIED_SUBJECT else LOCAL_ONLY
    )
    if mode not in _DEPLOYMENT_MODES:
        raise MemoryProvenanceError("invalid_provenance", "deployment mode is unsupported")
    if subject == UNIDENTIFIED_SUBJECT or mode != READ_CONTAINMENT:
        mode = LOCAL_ONLY
    caller_id = f"caller_{_keyed_digest(str(platform or 'unknown'), principal)[:40]}"
    return _new_ingress(
        caller_id=caller_id,
        subject_id=subject,
        origin=origin_value,
        authenticated_at=_format_timestamp(_utc(authenticated_at)),
        deployment_mode=mode,
        platform=str(platform or "unknown")[:40],
        profile=str(profile or "default")[:160],
        authenticated=True,
        reason_code="authenticated_ingress",
    )


def issue_synthetic_ingress(
    *,
    origin: str,
    reason: str = "synthetic_or_unidentified",
    platform: str = "synthetic",
    profile: str = "default",
    caller_class: str = "synthetic",
) -> TrustedMemoryIngress:
    origin_value = _safe_origin(origin)
    safe_class = re.sub(r"[^A-Za-z0-9._:-]", "_", str(caller_class or "synthetic"))[:80]
    caller_id = f"synthetic_{_keyed_digest(origin_value, safe_class)[:40]}"
    reason_code = re.sub(r"[^a-z0-9_:-]", "_", str(reason or "synthetic").lower())[:80]
    return _new_ingress(
        caller_id=caller_id,
        subject_id=UNIDENTIFIED_SUBJECT,
        origin=origin_value,
        authenticated_at=_format_timestamp(_utc()),
        deployment_mode=LOCAL_ONLY,
        platform=str(platform or "synthetic")[:40],
        profile=str(profile or "default")[:160],
        authenticated=False,
        reason_code=reason_code,
    )


def _issue_verified_proxy_ingress(payload: Mapping[str, Any]) -> TrustedMemoryIngress:
    caller_id = _safe_alias(payload.get("caller_id"), field_name="caller_id")
    subject_id = _safe_alias(payload.get("subject_id"), field_name="subject_id")
    origin = _safe_origin(payload.get("origin"))
    mode = str(payload.get("deployment_mode") or "")
    if mode not in _DEPLOYMENT_MODES:
        raise MemoryProvenanceError("proxy_payload_invalid", "proxy deployment mode is invalid")
    if subject_id == UNIDENTIFIED_SUBJECT:
        mode = LOCAL_ONLY
    authenticated_at = _format_timestamp(
        _parse_timestamp(payload.get("authenticated_at"), code="proxy_payload_invalid")
    )
    return _new_ingress(
        caller_id=caller_id,
        subject_id=subject_id,
        origin=origin,
        authenticated_at=authenticated_at,
        deployment_mode=mode,
        platform="proxy",
        profile="default",
        authenticated=True,
        reason_code="verified_proxy_proof",
    )


def bind_memory_ingress(ingress: TrustedMemoryIngress | None):
    """Bind a sealed ingress for one later turn-construction consume."""

    if ingress is not None and not _valid_ingress(ingress):
        ingress = issue_synthetic_ingress(origin="background", reason="invalid_ingress")
    return _CURRENT_INGRESS.set(ingress)


def clear_memory_ingress() -> None:
    _CURRENT_INGRESS.set(None)


def consume_memory_ingress() -> TrustedMemoryIngress | None:
    """Consume the ingress once; tools/providers must never read it ambiently."""

    value = _CURRENT_INGRESS.get()
    _CURRENT_INGRESS.set(None)
    return value if _valid_ingress(value) else None


@contextmanager
def memory_ingress_scope(ingress: TrustedMemoryIngress | None) -> Iterator[None]:
    token = bind_memory_ingress(ingress)
    try:
        yield
    finally:
        _CURRENT_INGRESS.reset(token)


def local_interactive_memory_ingress(
    *,
    origin: str,
    profile: str = "default",
    stdin: Any = None,
    stdout: Any = None,
) -> TrustedMemoryIngress:
    """Establish local-owner authority only at a real interactive TTY boundary."""

    import sys

    origin_value = _safe_origin(origin)
    if origin_value not in {"cli", "tui"}:
        raise MemoryProvenanceError("invalid_local_origin", "local origin must be cli or tui")
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    try:
        interactive = bool(input_stream.isatty()) and bool(output_stream.isatty())
    except Exception:
        interactive = False
    if not interactive:
        return issue_synthetic_ingress(
            origin=origin_value,
            reason="non_interactive_local_runtime",
            platform="local",
            profile=profile,
        )
    uid = str(getattr(os, "geteuid", lambda: "local")())
    principal_id = f"uid:{uid}"
    resolved_subject = UNIDENTIFIED_SUBJECT
    try:
        resolved_subject = load_memory_subject_bindings().resolve(
            "local", principal_id
        )
    except Exception:
        # A TTY proves local interactivity, not which private memory
        # subject it owns. Missing/unsafe bindings therefore stay local-only.
        resolved_subject = UNIDENTIFIED_SUBJECT
    return issue_authenticated_ingress(
        origin=origin_value,
        platform="local",
        principal_id=principal_id,
        subject_id=resolved_subject,
        profile=profile,
    )


@dataclass(frozen=True, slots=True, init=False)
class MemoryTurnProvenance:
    """Immutable v1 record plus sealed runtime-only turn bindings."""

    schema_version: str
    provenance_id: str
    caller_id: str
    subject_id: str
    origin: str
    authenticated_at: str
    deployment_mode: str
    _session_digest: str = field(repr=False)
    _turn_digest: str = field(repr=False)
    _message_digest: str = field(repr=False)
    _authenticated: bool = field(repr=False)
    _seal: str = field(repr=False)

    def __init__(self, capability: object, **values: Any) -> None:
        if capability is not _TURN_CAPABILITY:
            raise MemoryProvenanceError("untrusted_provenance", "turn provenance must be minted")
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @property
    def authorizes_private_memory(self) -> bool:
        return bool(
            self._authenticated
            and self.subject_id != UNIDENTIFIED_SUBJECT
            and self.deployment_mode == READ_CONTAINMENT
            and validate_memory_turn_provenance(self)
        )

    def to_protocol_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "provenance_id": self.provenance_id,
            "caller_id": self.caller_id,
            "subject_id": self.subject_id,
            "origin": self.origin,
            "authenticated_at": self.authenticated_at,
            "deployment_mode": self.deployment_mode,
        }


def _turn_seal_payload(provenance: MemoryTurnProvenance) -> dict[str, Any]:
    return {
        **provenance.to_protocol_dict(),
        "session_digest": provenance._session_digest,
        "turn_digest": provenance._turn_digest,
        "message_digest": provenance._message_digest,
        "authenticated": provenance._authenticated,
    }


def mint_turn_provenance(
    ingress: TrustedMemoryIngress | None,
    *,
    session_id: str,
    turn_id: str,
    message_id: str,
) -> MemoryTurnProvenance:
    if not _valid_ingress(ingress):
        ingress = issue_synthetic_ingress(origin="background", reason="missing_ingress")
    assert ingress is not None
    bindings = {
        "session_digest": _keyed_digest("session", str(session_id or "missing-session")),
        "turn_digest": _keyed_digest("turn", str(turn_id or "missing-turn")),
        "message_digest": _keyed_digest("message", str(message_id or "missing-message")),
    }
    values = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "provenance_id": str(uuid.uuid4()),
        "caller_id": ingress.caller_id,
        "subject_id": ingress.subject_id,
        "origin": ingress.origin,
        "authenticated_at": ingress.authenticated_at,
        "deployment_mode": ingress.deployment_mode,
        "_session_digest": bindings["session_digest"],
        "_turn_digest": bindings["turn_digest"],
        "_message_digest": bindings["message_digest"],
        "_authenticated": ingress.authenticated,
    }
    provisional = MemoryTurnProvenance(_TURN_CAPABILITY, **values, _seal="")
    return MemoryTurnProvenance(
        _TURN_CAPABILITY,
        **values,
        _seal=_seal_payload("turn", _turn_seal_payload(provisional)),
    )


def validate_memory_turn_provenance(
    provenance: Any,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    message_id: str | None = None,
) -> bool:
    if not isinstance(provenance, MemoryTurnProvenance):
        return False
    try:
        if provenance.schema_version != PROVENANCE_SCHEMA_VERSION:
            return False
        uuid.UUID(provenance.provenance_id)
        _safe_alias(provenance.caller_id, field_name="caller_id")
        _safe_alias(provenance.subject_id, field_name="subject_id")
        _safe_origin(provenance.origin)
        _parse_timestamp(provenance.authenticated_at, code="invalid_provenance")
        if provenance.deployment_mode not in _DEPLOYMENT_MODES:
            return False
        if not all(
            _SHA256_RE.fullmatch(value or "")
            for value in (
                provenance._session_digest,
                provenance._turn_digest,
                provenance._message_digest,
            )
        ):
            return False
        expected = _seal_payload("turn", _turn_seal_payload(provenance))
        if not hmac.compare_digest(expected, provenance._seal):
            return False
        checks = (
            (session_id, "session", provenance._session_digest),
            (turn_id, "turn", provenance._turn_digest),
            (message_id, "message", provenance._message_digest),
        )
        for raw, namespace, digest in checks:
            if raw is not None and not hmac.compare_digest(
                _keyed_digest(namespace, str(raw or f"missing-{namespace}")), digest
            ):
                return False
        return True
    except Exception:
        return False


def memory_boundary_kwargs(
    function: Any,
    *,
    memory_provenance: MemoryTurnProvenance | None,
    tool_call_id: str | None = None,
) -> dict[str, Any] | None:
    """Return explicit kwargs only when the boundary supports provenance.

    ``None`` means the optional memory path must be skipped/denied.  This
    prevents compatibility glue from silently invoking a legacy provider path
    without provenance.
    """

    if not validate_memory_turn_provenance(memory_provenance):
        return None
    try:
        import inspect

        signature = inspect.signature(function)
        parameters = signature.parameters
        # A catch-all **kwargs is not proof that the callee enforces this
        # contract; legacy providers often accept and ignore arbitrary kwargs.
        # Require the manager boundary to name the provenance explicitly.
        if "memory_provenance" not in parameters:
            return None
        kwargs: dict[str, Any] = {"memory_provenance": memory_provenance}
        if tool_call_id is not None:
            if "tool_call_id" not in parameters:
                return None
            kwargs["tool_call_id"] = tool_call_id
        return kwargs
    except (TypeError, ValueError):
        return None


def memory_denial_json(code: str = "memory_provenance_denied") -> str:
    return json.dumps(
        {
            "ok": False,
            "status": "denied",
            "error": {
                "code": code,
                "message": "Memory access is unavailable for this turn.",
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class _ProxyConfig:
    key: bytes = field(repr=False)
    key_id: str
    audience: str
    replay_path: Path


def _load_proxy_config() -> _ProxyConfig:
    key_path, key = _read_private_regular_file(
        os.getenv("HERMES_MEMORY_PROXY_PROOF_KEY_FILE"),
        context="memory proxy proof key",
        code="unsafe_proxy_proof_key",
        maximum_bytes=MAX_PROXY_KEY_BYTES,
    )
    del key_path
    if len(key) < 32:
        raise MemoryProvenanceError("unsafe_proxy_proof_key", "memory proxy proof key is too short")
    transport_key = str(os.getenv("GATEWAY_PROXY_KEY") or "").encode("utf-8")
    if transport_key and hmac.compare_digest(key.strip(), transport_key.strip()):
        raise MemoryProvenanceError(
            "proxy_key_reuse_denied",
            "memory proof signing requires a key distinct from proxy transport auth",
        )
    key_id = str(os.getenv("HERMES_MEMORY_PROXY_PROOF_KEY_ID") or "").strip()
    audience = str(os.getenv("HERMES_MEMORY_PROXY_PROOF_AUDIENCE") or "").strip()
    if not _SAFE_CONFIG_LABEL_RE.fullmatch(key_id) or not _SAFE_CONFIG_LABEL_RE.fullmatch(audience):
        raise MemoryProvenanceError("invalid_proxy_proof_config", "proxy key id/audience is invalid")
    replay_path = _require_absolute_path(
        os.getenv("HERMES_MEMORY_PROXY_REPLAY_STATE"), code="unsafe_proxy_replay_state"
    )
    if not replay_path.parent.is_dir():
        raise MemoryProvenanceError(
            "unsafe_proxy_replay_state", "proxy replay-state parent directory is missing"
        )
    try:
        parent_meta = replay_path.parent.stat()
        if not stat.S_ISDIR(parent_meta.st_mode):
            raise OSError
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and parent_meta.st_uid != geteuid():
            raise OSError
        # The verifier creates lock/temp/state files here.  A group/world
        # writable directory would let another principal swap those names.
        if parent_meta.st_mode & 0o022:
            raise OSError
    except OSError as exc:
        raise MemoryProvenanceError(
            "unsafe_proxy_replay_state", "proxy replay-state parent is invalid"
        ) from exc
    return _ProxyConfig(key=key, key_id=key_id, audience=audience, replay_path=replay_path)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str, *, code: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > MAX_PROXY_PROOF_BYTES:
        raise MemoryProvenanceError(code, "proxy proof encoding is invalid")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except Exception as exc:
        raise MemoryProvenanceError(code, "proxy proof encoding is invalid") from exc


def build_memory_proxy_proof(
    raw_body: bytes,
    *,
    method: str,
    path: str,
    ingress: TrustedMemoryIngress,
    now: datetime | None = None,
    lifetime_seconds: int = 30,
    nonce: str | None = None,
) -> str:
    """Sign one raw proxy request body using the distinct proof key."""

    if not _valid_ingress(ingress) or not ingress.authenticated:
        raise MemoryProvenanceError("untrusted_ingress", "proxy proof requires authenticated ingress")
    if ingress.subject_id == UNIDENTIFIED_SUBJECT or ingress.deployment_mode != READ_CONTAINMENT:
        raise MemoryProvenanceError("unidentified_subject", "proxy proof requires an identified subject")
    if not isinstance(raw_body, bytes):
        raise MemoryProvenanceError("proxy_body_invalid", "proxy body must be raw bytes")
    if not 1 <= int(lifetime_seconds) <= MAX_PROXY_PROOF_LIFETIME_SECONDS:
        raise MemoryProvenanceError("proxy_lifetime_invalid", "proxy proof lifetime is invalid")
    config = _load_proxy_config()
    current = _utc(now)
    nonce_value = nonce or _b64url_encode(secrets.token_bytes(24))
    if not _SAFE_NONCE_RE.fullmatch(nonce_value):
        raise MemoryProvenanceError("proxy_nonce_invalid", "proxy proof nonce is invalid")
    method_value = str(method or "").upper()
    path_value = str(path or "")
    if method_value != "POST" or not path_value.startswith("/") or len(path_value) > 512:
        raise MemoryProvenanceError("proxy_target_invalid", "proxy request target is invalid")
    issued_at = int(current.timestamp())
    payload = {
        "schema_version": PROXY_PROOF_SCHEMA_VERSION,
        "key_id": config.key_id,
        "audience": config.audience,
        "method": method_value,
        "path": path_value,
        "body_sha256": hashlib.sha256(raw_body).hexdigest(),
        "issued_at": issued_at,
        "expires_at": issued_at + int(lifetime_seconds),
        "nonce": nonce_value,
        "caller_id": ingress.caller_id,
        "subject_id": ingress.subject_id,
        "origin": ingress.origin,
        "authenticated_at": ingress.authenticated_at,
        "deployment_mode": ingress.deployment_mode,
    }
    signature = hmac.new(config.key, _canonical_json_bytes(payload), hashlib.sha256).digest()
    envelope = {"payload": payload, "signature": _b64url_encode(signature)}
    encoded = _b64url_encode(_canonical_json_bytes(envelope))
    if len(encoded) > MAX_PROXY_PROOF_BYTES:
        raise MemoryProvenanceError("proxy_proof_invalid", "proxy proof exceeds its byte bound")
    return encoded


def _read_replay_state(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    _path, raw = _read_private_regular_file(
        path,
        context="memory proxy replay state",
        code="unsafe_proxy_replay_state",
        maximum_bytes=MAX_PROXY_REPLAY_BYTES,
    )
    payload = _strict_json_object(
        raw, context="memory proxy replay state", max_bytes=MAX_PROXY_REPLAY_BYTES
    )
    if set(payload) != {"schema_version", "entries"} or payload.get("schema_version") != PROXY_REPLAY_SCHEMA_VERSION:
        raise MemoryProvenanceError("invalid_proxy_replay_state", "proxy replay state is invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_PROXY_REPLAY_ENTRIES:
        raise MemoryProvenanceError("invalid_proxy_replay_state", "proxy replay entries are invalid")
    normalized: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"nonce_digest", "expires_at"}:
            raise MemoryProvenanceError("invalid_proxy_replay_state", "proxy replay entry is invalid")
        digest = item.get("nonce_digest")
        expiry = item.get("expires_at")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or not isinstance(expiry, int):
            raise MemoryProvenanceError("invalid_proxy_replay_state", "proxy replay entry is invalid")
        normalized.append({"nonce_digest": digest, "expires_at": expiry})
    return normalized


def _atomic_write_replay_state(path: Path, entries: list[dict[str, Any]]) -> None:
    raw = _canonical_json_bytes(
        {"schema_version": PROXY_REPLAY_SCHEMA_VERSION, "entries": entries}
    )
    if len(raw) > MAX_PROXY_REPLAY_BYTES:
        raise MemoryProvenanceError("invalid_proxy_replay_state", "proxy replay state is too large")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _consume_proxy_nonce(path: Path, nonce: str, *, expires_at: int, now_epoch: int) -> None:
    try:
        import fcntl
    except ImportError as exc:
        raise MemoryProvenanceError(
            "proxy_replay_lock_unavailable", "durable proxy replay locking is unavailable"
        ) from exc
    lock_path = path.with_name(path.name + ".lock")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with _REPLAY_THREAD_LOCK:
        try:
            lock_fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise MemoryProvenanceError(
                "unsafe_proxy_replay_state", "proxy replay lock is unavailable"
            ) from exc
        try:
            os.fchmod(lock_fd, 0o600)
            _validate_private_stat(os.fstat(lock_fd), code="unsafe_proxy_replay_state")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MemoryProvenanceError(
                    "proxy_replay_lock_busy", "proxy replay lock is busy"
                ) from exc
            entries = [entry for entry in _read_replay_state(path) if entry["expires_at"] > now_epoch]
            nonce_digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
            if any(hmac.compare_digest(entry["nonce_digest"], nonce_digest) for entry in entries):
                raise MemoryProvenanceError("proxy_proof_replayed", "proxy proof was already consumed")
            if len(entries) >= MAX_PROXY_REPLAY_ENTRIES:
                # Never evict a still-live nonce to make room: doing so would
                # let the evicted proof replay inside its validity window.
                raise MemoryProvenanceError(
                    "proxy_replay_capacity_exceeded",
                    "proxy replay state cannot accept another live proof",
                )
            entries.append({"nonce_digest": nonce_digest, "expires_at": expires_at})
            entries.sort(key=lambda item: (item["expires_at"], item["nonce_digest"]))
            _atomic_write_replay_state(path, entries)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def verify_memory_proxy_proof(
    proof: str,
    raw_body: bytes,
    *,
    method: str,
    path: str,
    now: datetime | None = None,
) -> TrustedMemoryIngress:
    """Verify and durably consume one signed proxy provenance proof."""

    config = _load_proxy_config()
    if not isinstance(raw_body, bytes):
        raise MemoryProvenanceError("proxy_body_invalid", "proxy body must be raw bytes")
    envelope_raw = _b64url_decode(proof, code="proxy_proof_invalid")
    envelope = _strict_json_object(
        envelope_raw, context="memory proxy proof", max_bytes=MAX_PROXY_PROOF_BYTES
    )
    if set(envelope) != {"payload", "signature"} or not isinstance(envelope.get("payload"), dict):
        raise MemoryProvenanceError("proxy_proof_invalid", "proxy proof envelope is invalid")
    payload = envelope["payload"]
    required = {
        "schema_version",
        "key_id",
        "audience",
        "method",
        "path",
        "body_sha256",
        "issued_at",
        "expires_at",
        "nonce",
        "caller_id",
        "subject_id",
        "origin",
        "authenticated_at",
        "deployment_mode",
    }
    if set(payload) != required or payload.get("schema_version") != PROXY_PROOF_SCHEMA_VERSION:
        raise MemoryProvenanceError("proxy_payload_invalid", "proxy proof payload is invalid")
    if payload.get("key_id") != config.key_id:
        raise MemoryProvenanceError("proxy_key_id_mismatch", "proxy proof key id is not active")
    if payload.get("audience") != config.audience:
        raise MemoryProvenanceError("proxy_audience_mismatch", "proxy proof audience is invalid")
    supplied_signature = _b64url_decode(
        envelope.get("signature"), code="proxy_signature_invalid"
    )
    expected_signature = hmac.new(
        config.key, _canonical_json_bytes(payload), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise MemoryProvenanceError("proxy_signature_invalid", "proxy proof signature is invalid")
    method_value = str(method or "").upper()
    if payload.get("method") != method_value:
        raise MemoryProvenanceError("proxy_method_mismatch", "proxy proof method is invalid")
    if payload.get("path") != str(path or ""):
        raise MemoryProvenanceError("proxy_path_mismatch", "proxy proof path is invalid")
    digest = payload.get("body_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise MemoryProvenanceError("proxy_payload_invalid", "proxy body digest is invalid")
    if not hmac.compare_digest(digest, hashlib.sha256(raw_body).hexdigest()):
        raise MemoryProvenanceError("proxy_body_mismatch", "proxy request body does not match proof")
    issued_at = payload.get("issued_at")
    expires_at = payload.get("expires_at")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise MemoryProvenanceError("proxy_payload_invalid", "proxy proof times are invalid")
    if expires_at <= issued_at or expires_at - issued_at > MAX_PROXY_PROOF_LIFETIME_SECONDS:
        raise MemoryProvenanceError("proxy_lifetime_invalid", "proxy proof lifetime is invalid")
    now_epoch = int(_utc(now).timestamp())
    if issued_at > now_epoch + MAX_PROXY_CLOCK_SKEW_SECONDS:
        raise MemoryProvenanceError("proxy_proof_not_yet_valid", "proxy proof is not yet valid")
    if now_epoch >= expires_at:
        raise MemoryProvenanceError("proxy_proof_expired", "proxy proof has expired")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not _SAFE_NONCE_RE.fullmatch(nonce):
        raise MemoryProvenanceError("proxy_nonce_invalid", "proxy proof nonce is invalid")
    ingress = _issue_verified_proxy_ingress(payload)
    if not ingress.authenticated or ingress.subject_id == UNIDENTIFIED_SUBJECT:
        raise MemoryProvenanceError("proxy_subject_unidentified", "proxy proof subject is unidentified")
    _consume_proxy_nonce(
        config.replay_path,
        nonce,
        expires_at=expires_at,
        now_epoch=now_epoch,
    )
    return ingress


__all__ = [
    "LOCAL_ONLY",
    "MAX_PROXY_PROOF_LIFETIME_SECONDS",
    "MemoryProvenanceError",
    "MemorySubjectBindings",
    "MemoryTurnProvenance",
    "PROVENANCE_SCHEMA_VERSION",
    "PROXY_PROOF_HEADER",
    "READ_CONTAINMENT",
    "TrustedMemoryIngress",
    "UNIDENTIFIED_SUBJECT",
    "bind_memory_ingress",
    "build_memory_proxy_proof",
    "canonical_proxy_json_bytes",
    "canonical_proxy_json_text",
    "clear_memory_ingress",
    "consume_memory_ingress",
    "issue_authenticated_ingress",
    "issue_synthetic_ingress",
    "load_memory_subject_bindings",
    "local_interactive_memory_ingress",
    "memory_boundary_kwargs",
    "memory_denial_json",
    "memory_ingress_scope",
    "mint_turn_provenance",
    "validate_memory_turn_provenance",
    "verify_memory_proxy_proof",
]
