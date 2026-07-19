"""Fail-closed, offline verification for Memory V3 capability snapshots.

The safe boundary in this module always returns a typed decision.  Invalid,
expired, replayed, downgraded, or unavailable capability state disables
optional memory work while explicitly preserving the local chat path.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - the production target is POSIX
    _fcntl = None

from agent.memory_protocol import (
    MemoryProtocolError,
    MemoryRuntimeBinding,
    VENDORED_PROTOCOL_ROOT,
    canonical_json_bytes,
    canonical_json_digest,
    load_memory_runtime_binding,
    strict_json_loads,
)


SNAPSHOT_SCHEMA_VERSION = "memory-capability-snapshot/v1"
ENVELOPE_SCHEMA_VERSION = "memory-capability-envelope/v1"
SIGNATURE_ALGORITHM = "Ed25519"
SSHSIG_NAMESPACE = "memory-capability-v1"
SSHSIG_IDENTITY = "memory-capability"
MAX_SNAPSHOT_LIFETIME = timedelta(minutes=15)
MAX_SIGNATURE_BYTES = 32768
MAX_REPLAY_STATE_BYTES = 256 * 1024
MAX_REPLAY_SCOPES = 1024
MAX_REPLAY_STATE_VERSION = (1 << 63) - 1
REPLAY_LOCK_TIMEOUT_SECONDS = 0.5
REPLAY_LOCK_RETRY_SECONDS = 0.01
REPLAY_STATE_SCHEMA_VERSION = "memory-capability-replay/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ACTION_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

_ENVELOPE_FIELDS = {"schema_version", "protected", "snapshot", "signature"}
_PROTECTED_FIELDS = {"algorithm", "signature_encoding", "key_id"}
_SNAPSHOT_FIELDS = {
    "schema_version",
    "snapshot_id",
    "issuer",
    "audience",
    "destination",
    "deployment_mode",
    "security_epoch",
    "minimum_protocol_version",
    "minimum_policy_version",
    "capability_version",
    "runtime_manifest_digest",
    "config_digest",
    "protocol_bundle_digest",
    "policy_digest",
    "issued_at",
    "expires_at",
    "capabilities",
}
_CAPABILITY_FIELDS = {
    "local_reply",
    "existing_memory_read",
    "memory_tools_visible",
    "governed_write",
    "conversational_capture",
    "provider_create",
}


class CapabilityVerificationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason_code: str = "capability_unavailable",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason_code = reason_code
        self.retryable = retryable


@dataclass(frozen=True)
class ReadOnlyMemoryCapabilities:
    local_reply: bool
    existing_memory_read: bool
    memory_tools_visible: bool
    governed_write: bool
    conversational_capture: bool
    provider_create: bool


@dataclass(frozen=True)
class VerifiedMemoryCapabilitySnapshot:
    snapshot_id: str
    issuer: str
    audience: str
    destination: str
    deployment_mode: str
    security_epoch: int
    minimum_protocol_version: int
    minimum_policy_version: int
    capability_version: int
    runtime_manifest_digest: str
    config_digest: str
    protocol_bundle_digest: str
    policy_digest: str
    issued_at: datetime
    expires_at: datetime
    capabilities: ReadOnlyMemoryCapabilities
    envelope_digest: str


@dataclass(frozen=True)
class MemoryCapabilityDecision:
    outcome: str
    failure_code: str | None
    reason_code: str | None
    local_reply_allowed: bool
    retryable: bool
    snapshot: VerifiedMemoryCapabilitySnapshot | None
    runtime_manifest_digest: str | None
    policy_digest: str | None
    config_digest: str | None

    @property
    def memory_allowed(self) -> bool:
        return (
            self.outcome == "allow"
            and self.snapshot is not None
            and self.snapshot.capabilities.existing_memory_read
        )

    def to_denial_payload(
        self,
        *,
        action: str,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        if self.memory_allowed or self.reason_code is None:
            raise ValueError("an allowed capability decision is not a denial")
        if (
            type(action) is not str
            or len(action) > 100
            or ACTION_CODE_RE.fullmatch(action) is None
        ):
            raise ValueError("action must be a stable 1-100 character action code")
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return {
            "schema_version": "memory-denial/v1",
            "denial_id": str(uuid.uuid4()),
            "reason_code": self.reason_code,
            "action": action,
            "local_reply_allowed": True,
            "retryable": self.retryable,
            "created_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True)
class SnapshotWatermark:
    snapshot_id: str
    issued_at: datetime
    security_epoch: int
    minimum_protocol_version: int
    minimum_policy_version: int
    capability_version: int


class ReplayGuard(Protocol):
    def claim(
        self,
        snapshot: VerifiedMemoryCapabilitySnapshot,
        *,
        key_id: str,
    ) -> None: ...


def _snapshot_scope(
    snapshot: VerifiedMemoryCapabilitySnapshot,
    _key_id: str,
) -> tuple[str, str]:
    # Issuers and signing keys may rotate.  They must not reset the security
    # watermark for an audience/destination trust boundary.
    return (snapshot.audience, snapshot.destination)


def _next_watermark(
    previous: SnapshotWatermark | None,
    snapshot: VerifiedMemoryCapabilitySnapshot,
) -> SnapshotWatermark:
    if previous is not None:
        if snapshot.snapshot_id == previous.snapshot_id:
            raise CapabilityVerificationError(
                "snapshot_replay",
                "capability snapshot replay was denied",
                reason_code="security_downgrade",
            )
        if snapshot.issued_at <= previous.issued_at:
            raise CapabilityVerificationError(
                "valid_old_snapshot",
                "valid-old capability snapshot was denied",
                reason_code="security_downgrade",
            )
        current_floors = (
            snapshot.security_epoch,
            snapshot.minimum_protocol_version,
            snapshot.minimum_policy_version,
            snapshot.capability_version,
        )
        prior_floors = (
            previous.security_epoch,
            previous.minimum_protocol_version,
            previous.minimum_policy_version,
            previous.capability_version,
        )
        if any(current < prior for current, prior in zip(current_floors, prior_floors)):
            raise CapabilityVerificationError(
                "monotonic_floor_downgrade",
                "capability snapshot weakened an accepted monotonic floor",
                reason_code="security_downgrade",
            )
    return SnapshotWatermark(
        snapshot_id=snapshot.snapshot_id,
        issued_at=snapshot.issued_at,
        security_epoch=snapshot.security_epoch,
        minimum_protocol_version=snapshot.minimum_protocol_version,
        minimum_policy_version=snapshot.minimum_policy_version,
        capability_version=snapshot.capability_version,
    )


class SnapshotReplayGuard:
    """Explicit process-local guard for hermetic tests and bounded callers."""

    def __init__(
        self,
        initial: Mapping[tuple[str, str], SnapshotWatermark] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._watermarks = dict(initial or {})

    @staticmethod
    def _scope(
        snapshot: VerifiedMemoryCapabilitySnapshot,
        key_id: str,
    ) -> tuple[str, str]:
        return _snapshot_scope(snapshot, key_id)

    def claim(self, snapshot: VerifiedMemoryCapabilitySnapshot, *, key_id: str) -> None:
        scope = self._scope(snapshot, key_id)
        with self._lock:
            previous = self._watermarks.get(scope)
            self._watermarks[scope] = _next_watermark(previous, snapshot)

    def export(self) -> dict[tuple[str, str], SnapshotWatermark]:
        with self._lock:
            return dict(self._watermarks)


class DurableSnapshotReplayGuard:
    """Atomic cross-process replay guard backed by one bounded local JSON file."""

    _STATE_FIELDS = {"schema_version", "state_version", "watermarks"}
    _WATERMARK_FIELDS = {
        "audience",
        "destination",
        "snapshot_id",
        "issued_at",
        "security_epoch",
        "minimum_protocol_version",
        "minimum_policy_version",
        "capability_version",
    }

    def __init__(self, state_path: Path | str) -> None:
        self._state_path = Path(state_path)

    @staticmethod
    def _failure(code: str, message: str) -> CapabilityVerificationError:
        return CapabilityVerificationError(code, message, retryable=True)

    def _open_parent(self) -> tuple[int, str]:
        path = self._state_path
        if (
            not path.is_absolute()
            or path.name in {"", ".", ".."}
            or len(path.name) > 200
            or any(part in {"", ".", ".."} for part in path.parent.parts[1:])
        ):
            raise self._failure(
                "replay_state_path_invalid",
                "replay state requires an explicit safe absolute path",
            )
        nofollow = getattr(os, "O_NOFOLLOW", None)
        nonblock = getattr(os, "O_NONBLOCK", None)
        if nofollow is None or nonblock is None or _fcntl is None:
            raise self._failure(
                "replay_guard_lock_unavailable",
                "durable replay locking is unavailable",
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | nonblock
            | nofollow
        )
        descriptor = -1
        try:
            descriptor = os.open("/", flags)
            for part in path.parent.parts[1:]:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    os.close(next_descriptor)
                    raise OSError(errno.ENOTDIR, "replay parent is not a directory")
                os.close(descriptor)
                descriptor = next_descriptor
            parent_metadata = os.fstat(descriptor)
            if (
                parent_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(parent_metadata.st_mode) & 0o022
            ):
                raise OSError(errno.EACCES, "replay parent is not privately controlled")
            return descriptor, path.name
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise self._failure(
                "replay_state_path_unavailable",
                "replay state parent is unavailable or unsafe",
            ) from exc

    @staticmethod
    def _check_private_regular_file(descriptor: int, *, context: str) -> os.stat_result:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise DurableSnapshotReplayGuard._failure(
                "replay_state_path_invalid",
                f"{context} must be a private owned regular file",
            )
        return metadata

    @staticmethod
    def _read_all(descriptor: int, size: int) -> bytes:
        if size < 1 or size > MAX_REPLAY_STATE_BYTES:
            raise DurableSnapshotReplayGuard._failure(
                "replay_state_corrupt",
                "replay state exceeds its strict byte bound",
            )
        chunks: list[bytes] = []
        remaining = MAX_REPLAY_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != size or len(raw) > MAX_REPLAY_STATE_BYTES:
            raise DurableSnapshotReplayGuard._failure(
                "replay_state_corrupt",
                "replay state changed while it was being read",
            )
        return raw

    def _decode_state(
        self,
        raw: bytes,
    ) -> tuple[int, dict[tuple[str, str], SnapshotWatermark]]:
        try:
            row = strict_json_loads(
                raw,
                context="memory capability replay state",
                maximum_bytes=MAX_REPLAY_STATE_BYTES,
            )
            row = _exact_object(row, self._STATE_FIELDS, "replay state")
            if row["schema_version"] != REPLAY_STATE_SCHEMA_VERSION:
                raise ValueError("unsupported replay state schema")
            state_version = row["state_version"]
            if (
                not isinstance(state_version, int)
                or isinstance(state_version, bool)
                or state_version < 1
                or state_version > MAX_REPLAY_STATE_VERSION
            ):
                raise ValueError("invalid replay state version")
            encoded_watermarks = row["watermarks"]
            if (
                not isinstance(encoded_watermarks, list)
                or len(encoded_watermarks) > MAX_REPLAY_SCOPES
            ):
                raise ValueError("invalid replay watermark set")
            watermarks: dict[tuple[str, str], SnapshotWatermark] = {}
            for encoded in encoded_watermarks:
                encoded = _exact_object(encoded, self._WATERMARK_FIELDS, "replay watermark")
                audience = _bounded_text(encoded["audience"], "audience", 120)
                destination = _bounded_text(encoded["destination"], "destination", 160)
                scope = (audience, destination)
                if scope in watermarks:
                    raise ValueError("duplicate replay scope")
                integers = {
                    field: _integer(encoded[field], field)
                    for field in (
                        "security_epoch",
                        "minimum_protocol_version",
                        "minimum_policy_version",
                        "capability_version",
                    )
                }
                if any(value < 1 for value in integers.values()):
                    raise ValueError("invalid replay watermark floor")
                watermarks[scope] = SnapshotWatermark(
                    snapshot_id=_bounded_text(encoded["snapshot_id"], "snapshot_id", 120),
                    issued_at=_parse_time(encoded["issued_at"], "issued_at"),
                    **integers,
                )
            return state_version, watermarks
        except (MemoryProtocolError, CapabilityVerificationError, ValueError, TypeError) as exc:
            raise self._failure(
                "replay_state_corrupt",
                "replay state is invalid or corrupt",
            ) from exc

    def _read_state(
        self,
        parent_descriptor: int,
        state_name: str,
    ) -> tuple[int, dict[tuple[str, str], SnapshotWatermark], tuple[int, int, int, int] | None, str | None]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW")
            | getattr(os, "O_NONBLOCK")
        )
        try:
            descriptor = os.open(state_name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            return 0, {}, None, None
        except OSError as exc:
            raise self._failure(
                "replay_state_path_invalid",
                "replay state is unavailable or unsafe",
            ) from exc
        try:
            metadata = self._check_private_regular_file(descriptor, context="replay state")
            raw = self._read_all(descriptor, metadata.st_size)
            state_version, watermarks = self._decode_state(raw)
            token = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
            return state_version, watermarks, token, hashlib.sha256(raw).hexdigest()
        finally:
            os.close(descriptor)

    @staticmethod
    def _encode_state(
        state_version: int,
        watermarks: Mapping[tuple[str, str], SnapshotWatermark],
    ) -> bytes:
        rows = []
        for (audience, destination), watermark in sorted(watermarks.items()):
            rows.append(
                {
                    "audience": audience,
                    "destination": destination,
                    "snapshot_id": watermark.snapshot_id,
                    "issued_at": watermark.issued_at.astimezone(timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "security_epoch": watermark.security_epoch,
                    "minimum_protocol_version": watermark.minimum_protocol_version,
                    "minimum_policy_version": watermark.minimum_policy_version,
                    "capability_version": watermark.capability_version,
                }
            )
        raw = canonical_json_bytes(
            {
                "schema_version": REPLAY_STATE_SCHEMA_VERSION,
                "state_version": state_version,
                "watermarks": rows,
            }
        )
        if len(raw) > MAX_REPLAY_STATE_BYTES:
            raise DurableSnapshotReplayGuard._failure(
                "replay_state_capacity_exceeded",
                "replay state exceeds its strict byte bound",
            )
        return raw

    def _replace_state(
        self,
        parent_descriptor: int,
        state_name: str,
        *,
        expected_version: int,
        expected_token: tuple[int, int, int, int] | None,
        expected_digest: str | None,
        watermarks: Mapping[tuple[str, str], SnapshotWatermark],
    ) -> None:
        if expected_version >= MAX_REPLAY_STATE_VERSION:
            raise self._failure(
                "replay_state_capacity_exceeded",
                "replay state version cannot advance safely",
            )
        raw = self._encode_state(expected_version + 1, watermarks)
        temporary_name = f".{state_name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        temporary_exists = False
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW")
                | getattr(os, "O_NONBLOCK")
            )
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            temporary_exists = True
            self._check_private_regular_file(descriptor, context="temporary replay state")
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short replay state write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1

            observed_version, _, observed_token, observed_digest = self._read_state(
                parent_descriptor,
                state_name,
            )
            if (
                observed_version != expected_version
                or observed_token != expected_token
                or observed_digest != expected_digest
            ):
                raise self._failure(
                    "replay_state_cas_failed",
                    "replay state changed outside the held guard lock",
                )
            os.replace(
                temporary_name,
                state_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            temporary_exists = False
            os.fsync(parent_descriptor)
        except CapabilityVerificationError:
            raise
        except OSError as exc:
            raise self._failure(
                "replay_state_write_failed",
                "replay state could not be committed atomically",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except OSError:
                    pass

    @staticmethod
    def _acquire_lock(descriptor: int) -> None:
        if _fcntl is None or getattr(_fcntl, "LOCK_NB", None) is None:
            raise DurableSnapshotReplayGuard._failure(
                "replay_guard_lock_unavailable",
                "nonblocking durable replay locking is unavailable",
            )
        deadline = time.monotonic() + REPLAY_LOCK_TIMEOUT_SECONDS
        contention_errors = {errno.EACCES, errno.EAGAIN}
        if hasattr(errno, "EWOULDBLOCK"):
            contention_errors.add(errno.EWOULDBLOCK)
        while True:
            try:
                _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                return
            except OSError as exc:
                contended = exc.errno in contention_errors
                interrupted = exc.errno == errno.EINTR
                if not contended and not interrupted:
                    raise DurableSnapshotReplayGuard._failure(
                        "replay_guard_lock_unavailable",
                        "durable replay lock acquisition failed",
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    code = "replay_guard_contended" if contended else "replay_guard_lock_unavailable"
                    message = (
                        "durable replay lock remained contended"
                        if contended
                        else "durable replay lock acquisition was repeatedly interrupted"
                    )
                    raise DurableSnapshotReplayGuard._failure(code, message) from exc
                time.sleep(min(REPLAY_LOCK_RETRY_SECONDS, remaining))

    def claim(self, snapshot: VerifiedMemoryCapabilitySnapshot, *, key_id: str) -> None:
        parent_descriptor, state_name = self._open_parent()
        lock_descriptor = -1
        locked = False
        try:
            lock_name = f"{state_name}.lock"
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW")
                | getattr(os, "O_NONBLOCK")
            )
            lock_descriptor = os.open(
                lock_name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            self._check_private_regular_file(lock_descriptor, context="replay lock")
            self._acquire_lock(lock_descriptor)
            locked = True

            state_version, watermarks, token, digest = self._read_state(
                parent_descriptor,
                state_name,
            )
            scope = _snapshot_scope(snapshot, key_id)
            if scope not in watermarks and len(watermarks) >= MAX_REPLAY_SCOPES:
                raise self._failure(
                    "replay_state_capacity_exceeded",
                    "replay state cannot add another trust boundary",
                )
            watermarks[scope] = _next_watermark(watermarks.get(scope), snapshot)
            self._replace_state(
                parent_descriptor,
                state_name,
                expected_version=state_version,
                expected_token=token,
                expected_digest=digest,
                watermarks=watermarks,
            )
        except CapabilityVerificationError:
            raise
        except OSError as exc:
            raise self._failure(
                "replay_guard_unavailable",
                "durable replay protection is unavailable",
            ) from exc
        finally:
            cleanup_failure: CapabilityVerificationError | None = None
            cleanup_cause: OSError | None = None
            if locked:
                try:
                    _fcntl.flock(lock_descriptor, _fcntl.LOCK_UN)
                except OSError as exc:
                    cleanup_failure = self._failure(
                        "replay_guard_unlock_failed",
                        "durable replay protection could not release its lock",
                    )
                    cleanup_cause = exc
            if lock_descriptor >= 0:
                try:
                    os.close(lock_descriptor)
                except OSError as exc:
                    cleanup_failure = cleanup_failure or self._failure(
                        "replay_guard_unlock_failed",
                        "durable replay protection could not close its lock",
                    )
                    cleanup_cause = cleanup_cause or exc
            try:
                os.close(parent_descriptor)
            except OSError as exc:
                cleanup_failure = cleanup_failure or self._failure(
                    "replay_guard_unavailable",
                    "durable replay protection could not close its state directory",
                )
                cleanup_cause = cleanup_cause or exc
            if cleanup_failure is not None:
                raise cleanup_failure from cleanup_cause


def _exact_object(value: Any, fields: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CapabilityVerificationError(
            "schema_mismatch",
            f"{context} does not match the closed-world schema",
        )
    return value


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
    ):
        raise CapabilityVerificationError(
            "schema_mismatch",
            f"{field} must be bounded non-empty text",
        )
    return value


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CapabilityVerificationError("schema_mismatch", f"{field} must be an integer")
    return value


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise CapabilityVerificationError(
            "schema_mismatch",
            f"{field} must be an RFC3339 timestamp",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapabilityVerificationError(
            "schema_mismatch",
            f"{field} must be an RFC3339 timestamp",
        ) from exc
    if parsed.utcoffset() is None:
        raise CapabilityVerificationError("schema_mismatch", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _current_time(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise CapabilityVerificationError("schema_mismatch", "now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _decode_base64url(value: Any) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SIGNATURE_BYTES
        or not BASE64URL_RE.fullmatch(value)
        or len(value) % 4 == 1
    ):
        raise CapabilityVerificationError("signature_encoding_invalid", "signature is not base64url")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise CapabilityVerificationError(
            "signature_encoding_invalid",
            "signature is not base64url",
        ) from exc
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if canonical != value or not raw or len(raw) > MAX_SIGNATURE_BYTES:
        raise CapabilityVerificationError("signature_encoding_invalid", "signature is not canonical base64url")
    return raw


def _verify_raw_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(signature) != 64:
        return False
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if len(public_key) == 32:
            verifier = Ed25519PublicKey.from_public_bytes(public_key)
        elif public_key.lstrip().startswith(b"ssh-ed25519 "):
            verifier = serialization.load_ssh_public_key(public_key.strip())
        else:
            verifier = serialization.load_pem_public_key(public_key)
        if not isinstance(verifier, Ed25519PublicKey):
            return False
        verifier.verify(signature, message)
        return True
    except ImportError:
        # PyNaCl is present with the messaging extra.  The core Hermes package
        # declares cryptography, but this fallback keeps lean test/runtime
        # environments fail-safe for raw 32-byte verification keys.
        if len(public_key) != 32:
            return False
        try:
            from nacl.exceptions import BadSignatureError
            from nacl.signing import VerifyKey

            VerifyKey(public_key).verify(message, signature)
            return True
        except (ImportError, BadSignatureError, ValueError):
            return False
    except (TypeError, ValueError):
        return False
    except Exception as exc:
        # cryptography raises InvalidSignature from a dependency-specific type;
        # no verification exception is allowed across the local-chat boundary.
        if exc.__class__.__name__ == "InvalidSignature":
            return False
        return False


def _openssh_public_key_line(public_key: bytes) -> str | None:
    try:
        value = public_key.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if "\n" in value or "\r" in value or len(value) > 8192:
        return None
    parts = value.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        return None
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error):
        return None
    if not decoded:
        return None
    return value


def _verify_sshsig(public_key: bytes, message: bytes, signature: bytes) -> bool:
    key_line = _openssh_public_key_line(public_key)
    executable = shutil.which("ssh-keygen")
    if (
        key_line is None
        or executable is None
        or not signature.startswith(b"-----BEGIN SSH SIGNATURE-----\n")
        or not signature.rstrip().endswith(b"-----END SSH SIGNATURE-----")
    ):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-memory-sshsig-") as directory:
            root = Path(directory)
            allowed = root / "allowed_signers"
            signature_path = root / "snapshot.sig"
            allowed.write_text(f"{SSHSIG_IDENTITY} {key_line}\n", encoding="utf-8")
            signature_path.write_bytes(signature)
            result = subprocess.run(
                [
                    executable,
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed),
                    "-I",
                    SSHSIG_IDENTITY,
                    "-n",
                    SSHSIG_NAMESPACE,
                    "-s",
                    str(signature_path),
                ],
                input=message,
                capture_output=True,
                timeout=2,
                check=False,
            )
            return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _snapshot_identity(payload: Mapping[str, Any]) -> str:
    material = dict(payload)
    material.pop("snapshot_id", None)
    return f"cap-{hashlib.sha256(canonical_json_bytes(material)).hexdigest()[:24]}"


def _validate_snapshot(
    payload: Any,
    *,
    runtime: MemoryRuntimeBinding,
    now: datetime,
) -> tuple[VerifiedMemoryCapabilitySnapshot, dict[str, Any]]:
    row = _exact_object(payload, _SNAPSHOT_FIELDS, "capability snapshot")
    if row["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise CapabilityVerificationError("snapshot_schema_mismatch", "unsupported snapshot schema")
    snapshot_id = _bounded_text(row["snapshot_id"], "snapshot_id", 120)
    issuer = _bounded_text(row["issuer"], "issuer", 120)
    audience = _bounded_text(row["audience"], "audience", 120)
    destination = _bounded_text(row["destination"], "destination", 160)
    if issuer != runtime.capability_issuer:
        raise CapabilityVerificationError("issuer_mismatch", "capability issuer mismatch")
    if audience != runtime.audience:
        raise CapabilityVerificationError("audience_mismatch", "capability audience mismatch")
    if destination != runtime.destination:
        raise CapabilityVerificationError("destination_mismatch", "capability destination mismatch")
    if row["deployment_mode"] != runtime.deployment_mode:
        raise CapabilityVerificationError(
            "deployment_mode_mismatch",
            "capability deployment mode mismatch",
        )

    security_epoch = _integer(row["security_epoch"], "security_epoch")
    protocol_floor = _integer(row["minimum_protocol_version"], "minimum_protocol_version")
    policy_floor = _integer(row["minimum_policy_version"], "minimum_policy_version")
    capability_version = _integer(row["capability_version"], "capability_version")
    if security_epoch < runtime.security_epoch:
        raise CapabilityVerificationError(
            "security_epoch_downgrade",
            "capability security epoch is below the runtime floor",
            reason_code="security_downgrade",
        )
    if security_epoch > runtime.security_epoch:
        raise CapabilityVerificationError(
            "security_epoch_unsupported",
            "capability security epoch is not authenticated by the runtime bundle",
            reason_code="protocol_mismatch",
        )
    if protocol_floor < runtime.minimum_protocol_version:
        raise CapabilityVerificationError(
            "protocol_version_downgrade",
            "capability protocol version is below the runtime floor",
            reason_code="protocol_mismatch",
        )
    if protocol_floor not in runtime.supported_protocol_versions:
        raise CapabilityVerificationError(
            "protocol_version_unsupported",
            "capability requires an unsupported protocol version",
            reason_code="protocol_mismatch",
        )
    if policy_floor < runtime.minimum_policy_version:
        raise CapabilityVerificationError(
            "policy_version_downgrade",
            "capability policy version is below the runtime floor",
            reason_code="policy_mismatch",
        )
    if policy_floor not in runtime.supported_policy_versions:
        raise CapabilityVerificationError(
            "policy_version_unsupported",
            "capability requires an unsupported policy version",
            reason_code="policy_mismatch",
        )
    if capability_version < runtime.minimum_capability_version:
        raise CapabilityVerificationError(
            "capability_version_downgrade",
            "capability version is below the runtime floor",
            reason_code="security_downgrade",
        )
    if capability_version not in runtime.supported_capability_versions:
        raise CapabilityVerificationError(
            "capability_version_unsupported",
            "capability requires an unsupported capability version",
            reason_code="security_downgrade",
        )
    digests: dict[str, str] = {}
    for field, expected, failure_code, reason_code in (
        (
            "runtime_manifest_digest",
            runtime.manifest_digest,
            "runtime_manifest_digest_mismatch",
            "protocol_mismatch",
        ),
        ("config_digest", runtime.config_digest, "config_digest_mismatch", "policy_mismatch"),
        (
            "protocol_bundle_digest",
            runtime.protocol_bundle_digest,
            "protocol_bundle_digest_mismatch",
            "protocol_mismatch",
        ),
        ("policy_digest", runtime.policy_digest, "policy_digest_mismatch", "policy_mismatch"),
    ):
        value = row[field]
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise CapabilityVerificationError("schema_mismatch", f"{field} is invalid")
        if value != expected:
            raise CapabilityVerificationError(
                failure_code,
                f"capability {field.replace('_', ' ')} mismatch",
                reason_code=reason_code,
            )
        digests[field] = value

    capability_row = _exact_object(row["capabilities"], _CAPABILITY_FIELDS, "capabilities")
    if any(not isinstance(value, bool) for value in capability_row.values()):
        raise CapabilityVerificationError("schema_mismatch", "capability flags must be boolean")
    if capability_row["local_reply"] is not True:
        raise CapabilityVerificationError(
            "local_reply_disabled",
            "capability snapshot attempted to disable local reply",
        )
    if (
        capability_row["governed_write"]
        or capability_row["conversational_capture"]
        or capability_row["provider_create"]
    ):
        raise CapabilityVerificationError(
            "write_capability_forbidden",
            "read-only capability snapshot attempted to enable a write path",
            reason_code="write_contained",
        )

    issued_at = _parse_time(row["issued_at"], "issued_at")
    expires_at = _parse_time(row["expires_at"], "expires_at")
    if expires_at <= issued_at or expires_at - issued_at > MAX_SNAPSHOT_LIFETIME:
        raise CapabilityVerificationError(
            "snapshot_lifetime_invalid",
            "capability snapshot lifetime is invalid",
        )
    if now < issued_at:
        raise CapabilityVerificationError(
            "snapshot_not_yet_valid",
            "capability snapshot is not yet valid",
            retryable=True,
        )
    if now >= expires_at:
        raise CapabilityVerificationError(
            "snapshot_expired",
            "capability snapshot expired",
            reason_code="capability_expired",
            retryable=True,
        )
    if snapshot_id != _snapshot_identity(row):
        raise CapabilityVerificationError(
            "snapshot_identity_mismatch",
            "capability snapshot identity digest mismatch",
        )

    capabilities = ReadOnlyMemoryCapabilities(**capability_row)
    return (
        VerifiedMemoryCapabilitySnapshot(
            snapshot_id=snapshot_id,
            issuer=issuer,
            audience=audience,
            destination=destination,
            deployment_mode=row["deployment_mode"],
            security_epoch=security_epoch,
            minimum_protocol_version=protocol_floor,
            minimum_policy_version=policy_floor,
            capability_version=capability_version,
            runtime_manifest_digest=digests["runtime_manifest_digest"],
            config_digest=digests["config_digest"],
            protocol_bundle_digest=digests["protocol_bundle_digest"],
            policy_digest=digests["policy_digest"],
            issued_at=issued_at,
            expires_at=expires_at,
            capabilities=capabilities,
            envelope_digest="pending",
        ),
        row,
    )


def _protocol_failure(error: MemoryProtocolError) -> CapabilityVerificationError:
    if error.code in {"runtime_expired", "runtime_not_yet_valid"}:
        return CapabilityVerificationError(
            error.code,
            str(error),
            reason_code="capability_expired" if error.code == "runtime_expired" else "capability_unavailable",
            retryable=True,
        )
    if "policy" in error.code or "config" in error.code:
        return CapabilityVerificationError(
            error.code,
            str(error),
            reason_code="policy_mismatch",
        )
    if "downgrade" in error.code:
        return CapabilityVerificationError(
            error.code,
            str(error),
            reason_code="security_downgrade",
        )
    return CapabilityVerificationError(
        error.code,
        str(error),
        reason_code="protocol_mismatch",
    )


class MemoryCapabilityVerifier:
    """Pre-bound verifier whose public ``verify`` method never raises."""

    def __init__(
        self,
        *,
        runtime: MemoryRuntimeBinding | None,
        public_key: bytes | None,
        replay_guard: ReplayGuard | None,
        bootstrap_failure: CapabilityVerificationError | None,
    ) -> None:
        self._runtime = runtime
        self._public_key = public_key
        self._replay_guard = replay_guard
        self._bootstrap_failure = bootstrap_failure

    @classmethod
    def from_frozen_runtime_manifest(
        cls,
        runtime_manifest: bytes | str | Mapping[str, Any],
        *,
        expected_manifest_digest: str,
        observed_policy_digest: str,
        observed_config_digest: str,
        public_key: bytes | str,
        now: datetime | None = None,
        replay_guard: ReplayGuard | None = None,
        protocol_root: Path | str = VENDORED_PROTOCOL_ROOT,
    ) -> "MemoryCapabilityVerifier":
        try:
            current = _current_time(now)
            if replay_guard is None:
                raise CapabilityVerificationError(
                    "replay_guard_required",
                    "an explicit replay guard is required",
                )
            if isinstance(public_key, str):
                key_bytes = public_key.encode("utf-8")
            elif isinstance(public_key, bytes):
                key_bytes = bytes(public_key)
            else:
                raise CapabilityVerificationError(
                    "public_key_invalid",
                    "capability verification key must be bytes or text",
                )
            if not key_bytes or len(key_bytes) > 64 * 1024:
                raise CapabilityVerificationError(
                    "public_key_invalid",
                    "capability verification key is empty or oversized",
                )
            runtime = load_memory_runtime_binding(
                runtime_manifest,
                expected_manifest_digest=expected_manifest_digest,
                observed_policy_digest=observed_policy_digest,
                observed_config_digest=observed_config_digest,
                now=current,
                protocol_root=protocol_root,
            )
            if hashlib.sha256(key_bytes).hexdigest() != runtime.capability_public_key_digest:
                raise CapabilityVerificationError(
                    "public_key_digest_mismatch",
                    "capability verification key does not match the frozen runtime target",
                )
            return cls(
                runtime=runtime,
                public_key=key_bytes,
                replay_guard=replay_guard,
                bootstrap_failure=None,
            )
        except MemoryProtocolError as exc:
            return cls(
                runtime=None,
                public_key=None,
                replay_guard=replay_guard,
                bootstrap_failure=_protocol_failure(exc),
            )
        except CapabilityVerificationError as exc:
            return cls(
                runtime=None,
                public_key=None,
                replay_guard=replay_guard,
                bootstrap_failure=exc,
            )
        except Exception:
            return cls(
                runtime=None,
                public_key=None,
                replay_guard=replay_guard,
                bootstrap_failure=CapabilityVerificationError(
                    "verifier_bootstrap_failed",
                    "capability verifier bootstrap failed",
                ),
            )

    def _denied(self, failure: CapabilityVerificationError) -> MemoryCapabilityDecision:
        runtime = self._runtime
        return MemoryCapabilityDecision(
            outcome="local_only",
            failure_code=failure.code,
            reason_code=failure.reason_code,
            local_reply_allowed=True,
            retryable=failure.retryable,
            snapshot=None,
            runtime_manifest_digest=runtime.manifest_digest if runtime else None,
            policy_digest=runtime.policy_digest if runtime else None,
            config_digest=runtime.config_digest if runtime else None,
        )

    def verify(
        self,
        envelope: bytes | str | Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> MemoryCapabilityDecision:
        try:
            if self._bootstrap_failure is not None:
                return self._denied(self._bootstrap_failure)
            if (
                self._runtime is None
                or self._public_key is None
                or self._replay_guard is None
            ):
                return self._denied(
                    CapabilityVerificationError(
                        "verifier_unavailable",
                        "capability verifier is unavailable",
                    )
                )
            current = _current_time(now)
            try:
                self._runtime.require_fresh(current)
            except MemoryProtocolError as exc:
                return self._denied(_protocol_failure(exc))

            row = strict_json_loads(envelope, context="capability envelope", maximum_bytes=256 * 1024)
            row = _exact_object(row, _ENVELOPE_FIELDS, "capability envelope")
            if row["schema_version"] != ENVELOPE_SCHEMA_VERSION:
                raise CapabilityVerificationError(
                    "envelope_schema_mismatch",
                    "unsupported capability envelope schema",
                )
            protected = _exact_object(row["protected"], _PROTECTED_FIELDS, "protected header")
            if protected["algorithm"] != SIGNATURE_ALGORITHM:
                raise CapabilityVerificationError(
                    "signature_algorithm_mismatch",
                    "unsupported capability signature algorithm",
                )
            signature_encoding = protected["signature_encoding"]
            if signature_encoding not in {"ed25519-raw", "sshsig"}:
                raise CapabilityVerificationError(
                    "signature_encoding_mismatch",
                    "unsupported capability signature encoding",
                )
            key_id = _bounded_text(protected["key_id"], "key_id", 120)
            if key_id != self._runtime.capability_key_id:
                raise CapabilityVerificationError(
                    "key_identity_mismatch",
                    "capability signing key identity mismatch",
                )

            snapshot, snapshot_row = _validate_snapshot(
                row["snapshot"],
                runtime=self._runtime,
                now=current,
            )
            signature = _decode_base64url(row["signature"])
            signed_material = canonical_json_bytes(
                {"protected": protected, "snapshot": snapshot_row}
            )
            if signature_encoding == "ed25519-raw":
                signature_valid = _verify_raw_ed25519(
                    self._public_key,
                    signed_material,
                    signature,
                )
            else:
                signature_valid = _verify_sshsig(
                    self._public_key,
                    signed_material,
                    signature,
                )
            if not signature_valid:
                raise CapabilityVerificationError(
                    "signature_invalid",
                    "capability signature verification failed",
                )

            snapshot = replace(snapshot, envelope_digest=canonical_json_digest(row))
            self._replay_guard.claim(snapshot, key_id=key_id)
            return MemoryCapabilityDecision(
                outcome="allow",
                failure_code=None,
                reason_code=None,
                local_reply_allowed=True,
                retryable=False,
                snapshot=snapshot,
                runtime_manifest_digest=self._runtime.manifest_digest,
                policy_digest=self._runtime.policy_digest,
                config_digest=self._runtime.config_digest,
            )
        except MemoryProtocolError as exc:
            return self._denied(_protocol_failure(exc))
        except CapabilityVerificationError as exc:
            return self._denied(exc)
        except Exception:
            return self._denied(
                CapabilityVerificationError(
                    "verification_failed",
                    "capability verification failed safely",
                )
            )


__all__ = [
    "CapabilityVerificationError",
    "DurableSnapshotReplayGuard",
    "MemoryCapabilityDecision",
    "MemoryCapabilityVerifier",
    "ReadOnlyMemoryCapabilities",
    "ReplayGuard",
    "SnapshotReplayGuard",
    "SnapshotWatermark",
    "VerifiedMemoryCapabilitySnapshot",
]
