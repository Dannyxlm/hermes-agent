"""Fail-closed Memory V3 runtime binding for Hermes.

This module is the provider-neutral bridge between the frozen CloudSeed
release contract and Hermes' ``MemoryManager``.  It deliberately has no
network client: all chat-path decisions use local, atomically published files.

The runtime is opt-in.  Unless ``HERMES_MEMORY_V3_CONFIG_FILE`` is present,
callers should preserve the upstream/legacy memory behavior unchanged.  Once
that variable is present, missing or malformed companion configuration is an
explicit fail-closed Memory V3 deployment rather than a fallback to legacy
provider behavior.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from agent.memory_capability_snapshot import (
    DurableSnapshotReplayGuard,
    MemoryCapabilityDecision,
    MemoryCapabilityVerifier,
)
from agent.memory_protocol import canonical_json_digest, strict_json_loads
from agent.memory_provenance import (
    MemoryTurnProvenance,
    load_memory_subject_bindings,
    validate_memory_turn_provenance,
)


CONFIG_SCHEMA_VERSION = "hermes-memory-v3-config/v1"
POLICY_SCHEMA_VERSION = "hermes-memory-readonly-policy/v1"
DEPLOYMENT_MODE = "tools_only_read_containment"

CONFIG_ENV = "HERMES_MEMORY_V3_CONFIG_FILE"
RUNTIME_MANIFEST_ENV = "HERMES_MEMORY_V3_RUNTIME_MANIFEST_FILE"
RUNTIME_MANIFEST_DIGEST_ENV = "HERMES_MEMORY_V3_RUNTIME_MANIFEST_DIGEST"
POLICY_ENV = "HERMES_MEMORY_V3_POLICY_FILE"
SNAPSHOT_ENV = "HERMES_MEMORY_V3_CAPABILITY_SNAPSHOT_FILE"
PUBLIC_KEY_ENV = "HERMES_MEMORY_V3_CAPABILITY_PUBLIC_KEY_FILE"
REPLAY_STATE_ENV = "HERMES_MEMORY_V3_REPLAY_STATE_FILE"
SUBJECT_BINDINGS_ENV = "HERMES_MEMORY_SUBJECT_BINDINGS_FILE"

_REQUIRED_ENV = (
    CONFIG_ENV,
    RUNTIME_MANIFEST_ENV,
    RUNTIME_MANIFEST_DIGEST_ENV,
    POLICY_ENV,
    SNAPSHOT_ENV,
    PUBLIC_KEY_ENV,
    REPLAY_STATE_ENV,
    SUBJECT_BINDINGS_ENV,
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SAFE_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_SAFE_TARGET_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SENSITIVE_TARGET_KEY_RE = re.compile(
    r"(?:api_?key|token|secret|password|credential|private_?key)", re.IGNORECASE
)

# Private memory is never available to a group, webhook, cron, background,
# restored, or delegated origin, even if a deployment file accidentally names
# one.  API/Desktop origins still need authenticated, sealed provenance.
_PRIVATE_READ_ORIGINS = frozenset(
    {"telegram_private", "cli", "tui", "photon_api", "desktop_websocket"}
)

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_POLICY_BYTES = 32 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 256 * 1024
_MAX_PUBLIC_KEY_BYTES = 64 * 1024
_MAX_PROVENANCE_AGE_SECONDS = 5 * 60
_MAX_PROVENANCE_FUTURE_SKEW_SECONDS = 5


class MemoryRuntimeError(ValueError):
    """A content-safe bootstrap or local snapshot failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if _SAFE_CODE_RE.fullmatch(code) else "memory_runtime_invalid"


@dataclass(frozen=True, slots=True)
class MemoryReadAuthorization:
    allowed: bool
    code: str
    provider_name: str


@dataclass(frozen=True, slots=True)
class _RuntimeConfig:
    provider_name: str
    subject_id: str
    allowed_origins: frozenset[str]
    subject_bindings_digest: str
    provider_target: Mapping[str, str]
    provider_limits: Mapping[str, int | float]
    observed_digest: str


@dataclass(frozen=True, slots=True)
class _RuntimePolicy:
    capability_ceiling: Mapping[str, bool]
    observed_digest: str


def _safe_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise MemoryRuntimeError("runtime_time_invalid", "runtime time must include a timezone")
    return current.astimezone(timezone.utc)


def _parse_time(value: Any, *, code: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MemoryRuntimeError(code, "runtime timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryRuntimeError(code, "runtime timestamp is invalid") from exc
    return _safe_now(parsed)


def _absolute_path(value: Any, *, code: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MemoryRuntimeError(code, "an explicit absolute file path is required")
    path = Path(value)
    if (
        not path.is_absolute()
        or path.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise MemoryRuntimeError(code, "the configured file path is unsafe")
    return path


def _read_regular_file(
    value: Any,
    *,
    code: str,
    maximum_bytes: int,
    private: bool = False,
) -> tuple[Path, bytes]:
    """Read one bounded non-symlink regular file without FIFO blocking."""

    path = _absolute_path(value, code=code)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise MemoryRuntimeError(code, "non-symlink file reads are unavailable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | nofollow
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        allowed_owners = {getattr(os, "geteuid", lambda: before.st_uid)(), 0}
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in allowed_owners
            or mode & (0o077 if private else 0o022)
            or before.st_size < 1
            or before.st_size > maximum_bytes
        ):
            raise MemoryRuntimeError(code, "the configured file is unavailable or unsafe")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if (
            not raw
            or len(raw) > maximum_bytes
            or len(raw) != before.st_size
            or identity_before != identity_after
        ):
            raise MemoryRuntimeError(code, "the configured file changed while being read")
        return path, raw
    except MemoryRuntimeError:
        raise
    except OSError as exc:
        raise MemoryRuntimeError(code, "the configured file is unavailable or unsafe") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _exact_object(value: Any, fields: set[str], *, code: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise MemoryRuntimeError(code, "runtime configuration fields are invalid")
    return value


def _safe_digest(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MemoryRuntimeError(code, "runtime digest is invalid")
    return value


def _load_config(raw: bytes, *, bindings_digest: str) -> _RuntimeConfig:
    try:
        payload = strict_json_loads(
            raw,
            context="Hermes Memory V3 configuration",
            maximum_bytes=_MAX_CONFIG_BYTES,
        )
    except Exception as exc:
        raise MemoryRuntimeError("memory_config_invalid", "memory configuration is invalid") from exc
    row = _exact_object(
        payload,
        {
            "schema_version",
            "deployment_mode",
            "subject_id",
            "allowed_origins",
            "subject_bindings_digest",
            "provider",
        },
        code="memory_config_invalid",
    )
    if row["schema_version"] != CONFIG_SCHEMA_VERSION or row["deployment_mode"] != DEPLOYMENT_MODE:
        raise MemoryRuntimeError("memory_config_invalid", "memory configuration version is unsupported")
    subject_id = row["subject_id"]
    if not isinstance(subject_id, str) or _SAFE_SUBJECT_RE.fullmatch(subject_id) is None:
        raise MemoryRuntimeError("memory_config_invalid", "memory subject binding is invalid")
    expected_bindings_digest = _safe_digest(
        row["subject_bindings_digest"], code="memory_config_invalid"
    )
    if expected_bindings_digest != bindings_digest:
        raise MemoryRuntimeError(
            "subject_bindings_digest_mismatch", "memory subject bindings changed"
        )

    origins = row["allowed_origins"]
    if (
        not isinstance(origins, list)
        or not origins
        or len(origins) > len(_PRIVATE_READ_ORIGINS)
        or any(type(origin) is not str for origin in origins)
        or len(set(origins)) != len(origins)
        or not set(origins).issubset(_PRIVATE_READ_ORIGINS)
    ):
        raise MemoryRuntimeError("memory_config_invalid", "private read origins are invalid")

    provider = _exact_object(
        row["provider"], {"name", "target", "limits"}, code="memory_config_invalid"
    )
    provider_name = provider["name"]
    if not isinstance(provider_name, str) or _SAFE_NAME_RE.fullmatch(provider_name) is None:
        raise MemoryRuntimeError("memory_config_invalid", "memory provider name is invalid")

    target = provider["target"]
    if (
        type(target) is not dict
        or not target
        or len(target) > 32
        or any(
            not isinstance(key, str)
            or _SAFE_TARGET_KEY_RE.fullmatch(key) is None
            or _SENSITIVE_TARGET_KEY_RE.search(key) is not None
            or not isinstance(value, str)
            or not value
            or len(value) > 512
            for key, value in target.items()
        )
    ):
        raise MemoryRuntimeError("memory_config_invalid", "memory provider target is invalid")

    limits = _exact_object(
        provider["limits"],
        {"deadline_seconds", "max_provider_calls", "max_items", "max_chars"},
        code="memory_config_invalid",
    )
    deadline = limits["deadline_seconds"]
    max_calls = limits["max_provider_calls"]
    max_items = limits["max_items"]
    max_chars = limits["max_chars"]
    if (
        not isinstance(deadline, (int, float))
        or isinstance(deadline, bool)
        or not 0.05 <= float(deadline) <= 5.0
        or type(max_calls) is not int
        or not 1 <= max_calls <= 4
        or type(max_items) is not int
        or not 1 <= max_items <= 16
        or type(max_chars) is not int
        or not 1 <= max_chars <= 8000
    ):
        raise MemoryRuntimeError("memory_config_invalid", "memory provider limits are invalid")

    return _RuntimeConfig(
        provider_name=provider_name,
        subject_id=subject_id,
        allowed_origins=frozenset(origins),
        subject_bindings_digest=bindings_digest,
        provider_target=dict(target),
        provider_limits={
            "deadline_seconds": float(deadline),
            "max_provider_calls": max_calls,
            "max_items": max_items,
            "max_chars": max_chars,
        },
        observed_digest=canonical_json_digest(row),
    )


def _load_policy(raw: bytes) -> _RuntimePolicy:
    try:
        payload = strict_json_loads(
            raw,
            context="Hermes Memory V3 read-only policy",
            maximum_bytes=_MAX_POLICY_BYTES,
        )
    except Exception as exc:
        raise MemoryRuntimeError("memory_policy_invalid", "memory policy is invalid") from exc
    fields = {
        "schema_version",
        "deployment_mode",
        "local_reply",
        "existing_memory_read",
        "memory_tools_visible",
        "governed_write",
        "conversational_capture",
        "provider_create",
    }
    row = _exact_object(payload, fields, code="memory_policy_invalid")
    if row["schema_version"] != POLICY_SCHEMA_VERSION or row["deployment_mode"] != DEPLOYMENT_MODE:
        raise MemoryRuntimeError("memory_policy_invalid", "memory policy version is unsupported")
    boolean_fields = fields - {"schema_version", "deployment_mode"}
    if any(type(row[field]) is not bool for field in boolean_fields):
        raise MemoryRuntimeError("memory_policy_invalid", "memory policy flags are invalid")
    required = {
        "local_reply": True,
        "existing_memory_read": True,
        "memory_tools_visible": True,
        "governed_write": False,
        "conversational_capture": False,
        "provider_create": False,
    }
    if any(row[key] is not value for key, value in required.items()):
        raise MemoryRuntimeError("memory_policy_not_readonly", "memory policy is not read-only")
    return _RuntimePolicy(
        capability_ceiling={key: row[key] for key in required},
        observed_digest=canonical_json_digest(row),
    )


def _local_denial(code: str) -> MemoryCapabilityDecision:
    safe_code = (
        code
        if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code)
        else "memory_runtime_unavailable"
    )
    return MemoryCapabilityDecision(
        outcome="local_only",
        failure_code=safe_code,
        reason_code="capability_unavailable",
        local_reply_allowed=True,
        retryable=False,
        snapshot=None,
        runtime_manifest_digest=None,
        policy_digest=None,
        config_digest=None,
    )


class MemoryRuntimeController:
    """One fail-closed, locally verified Memory V3 runtime generation."""

    def __init__(self, environ: Mapping[str, str] | None = None, *, now: datetime | None = None) -> None:
        env: Mapping[str, str] = os.environ if environ is None else environ
        self.active = bool(str(env.get(CONFIG_ENV, "") or "").strip())
        self._bootstrap_failure = ""
        self._config: _RuntimeConfig | None = None
        self._policy: _RuntimePolicy | None = None
        self._verifier: MemoryCapabilityVerifier | None = None
        self._snapshot_path: Path | None = None
        self._subject_bindings_path: Path | None = None
        self._runtime_manifest_path: Path | None = None
        self._runtime_manifest_digest_path: Path | None = None
        self._runtime_manifest_digest = ""
        self._runtime_public_key = b""
        self._replay_guard: DurableSnapshotReplayGuard | None = None
        self._runtime_expires_at: datetime | None = None
        self._generation_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cached_snapshot_digest = ""
        self._cached_decision: MemoryCapabilityDecision | None = None
        self._cached_valid_until: datetime | None = None
        if not self.active:
            return
        try:
            missing = [name for name in _REQUIRED_ENV if not str(env.get(name, "") or "").strip()]
            if missing:
                raise MemoryRuntimeError(
                    "memory_runtime_config_incomplete",
                    "Memory V3 runtime configuration is incomplete",
                )
            current = _safe_now(now)
            _config_path, config_raw = _read_regular_file(
                env[CONFIG_ENV],
                code="memory_config_unavailable",
                maximum_bytes=_MAX_CONFIG_BYTES,
                private=True,
            )
            _policy_path, policy_raw = _read_regular_file(
                env[POLICY_ENV],
                code="memory_policy_unavailable",
                maximum_bytes=_MAX_POLICY_BYTES,
            )
            _manifest_path, manifest_raw = _read_regular_file(
                env[RUNTIME_MANIFEST_ENV],
                code="memory_manifest_unavailable",
                maximum_bytes=_MAX_MANIFEST_BYTES,
            )
            snapshot_path = _absolute_path(
                env[SNAPSHOT_ENV], code="memory_snapshot_path_invalid"
            )
            _key_path, public_key = _read_regular_file(
                env[PUBLIC_KEY_ENV],
                code="memory_public_key_unavailable",
                maximum_bytes=_MAX_PUBLIC_KEY_BYTES,
            )

            bindings = load_memory_subject_bindings(env[SUBJECT_BINDINGS_ENV])
            config = _load_config(config_raw, bindings_digest=bindings.content_digest)
            policy = _load_policy(policy_raw)
            digest_path = _manifest_path.with_suffix(".sha256")
            expected_manifest_digest = self._read_manifest_digest(
                digest_path,
                fallback=env[RUNTIME_MANIFEST_DIGEST_ENV],
            )
            try:
                manifest = strict_json_loads(
                    manifest_raw,
                    context="Hermes Memory V3 runtime manifest",
                    maximum_bytes=_MAX_MANIFEST_BYTES,
                )
                if type(manifest) is not dict:
                    raise TypeError("manifest must be an object")
                runtime_expires_at = _parse_time(
                    manifest.get("expires_at"), code="memory_manifest_time_invalid"
                )
            except MemoryRuntimeError:
                raise
            except Exception as exc:
                raise MemoryRuntimeError(
                    "memory_manifest_invalid", "memory runtime manifest is invalid"
                ) from exc

            replay_guard = DurableSnapshotReplayGuard(env[REPLAY_STATE_ENV])
            verifier = MemoryCapabilityVerifier.from_frozen_runtime_manifest(
                manifest_raw,
                expected_manifest_digest=expected_manifest_digest,
                observed_policy_digest=policy.observed_digest,
                observed_config_digest=config.observed_digest,
                public_key=public_key,
                now=current,
                replay_guard=replay_guard,
            )
            self._config = config
            self._policy = policy
            self._verifier = verifier
            self._snapshot_path = snapshot_path
            self._subject_bindings_path = _absolute_path(
                env[SUBJECT_BINDINGS_ENV], code="unsafe_subject_bindings_file"
            )
            self._runtime_manifest_path = _manifest_path
            self._runtime_manifest_digest_path = digest_path
            self._runtime_manifest_digest = expected_manifest_digest
            self._runtime_public_key = public_key
            self._replay_guard = replay_guard
            self._runtime_expires_at = runtime_expires_at
        except Exception as exc:
            code = getattr(exc, "code", "memory_runtime_bootstrap_failed")
            self._bootstrap_failure = (
                code if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code) else "memory_runtime_bootstrap_failed"
            )

    @property
    def bootstrap_failure_code(self) -> str:
        return self._bootstrap_failure

    @property
    def provider_name(self) -> str:
        return self._config.provider_name if self._config is not None else ""

    @property
    def provider_target(self) -> dict[str, str]:
        return dict(self._config.provider_target) if self._config is not None else {}

    @property
    def provider_capability_ceiling(self) -> dict[str, bool | int | float]:
        if self._policy is None or self._config is None:
            return {
                "existing_memory_read": False,
                "memory_tools_visible": False,
                "governed_write": False,
                "conversational_capture": False,
                "provider_create": False,
                "deadline_seconds": 1.0,
                "max_provider_calls": 1,
                "max_items": 1,
                "max_chars": 1,
            }
        grant: MutableMapping[str, bool | int | float] = dict(self._policy.capability_ceiling)
        grant.update(self._config.provider_limits)
        return dict(grant)

    @staticmethod
    def _read_manifest_digest(path: Path, *, fallback: str) -> str:
        if not path.exists():
            return _safe_digest(fallback, code="memory_manifest_digest_invalid")
        _path, raw = _read_regular_file(
            str(path),
            code="memory_manifest_digest_invalid",
            maximum_bytes=128,
        )
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise MemoryRuntimeError(
                "memory_manifest_digest_invalid", "runtime manifest digest is invalid"
            ) from exc
        return _safe_digest(value, code="memory_manifest_digest_invalid")

    def _refresh_runtime_generation(self, current: datetime) -> str | None:
        """Hot-bind an atomically published manifest generation when it changes."""

        path = self._runtime_manifest_path
        digest_path = self._runtime_manifest_digest_path
        policy = self._policy
        config = self._config
        replay_guard = self._replay_guard
        if (
            path is None
            or digest_path is None
            or policy is None
            or config is None
            or replay_guard is None
            or not self._runtime_public_key
            or not digest_path.exists()
        ):
            return None
        try:
            expected = self._read_manifest_digest(
                digest_path,
                fallback=self._runtime_manifest_digest,
            )
        except Exception as exc:
            return getattr(exc, "code", "memory_manifest_digest_invalid")
        if expected == self._runtime_manifest_digest:
            return None

        with self._generation_lock:
            if expected == self._runtime_manifest_digest:
                return None
            try:
                _path, raw = _read_regular_file(
                    str(path),
                    code="memory_manifest_unavailable",
                    maximum_bytes=_MAX_MANIFEST_BYTES,
                )
                manifest = strict_json_loads(
                    raw,
                    context="Hermes Memory V3 runtime manifest",
                    maximum_bytes=_MAX_MANIFEST_BYTES,
                )
                runtime_expires_at = _parse_time(
                    manifest.get("expires_at"), code="memory_manifest_time_invalid"
                )
                verifier = MemoryCapabilityVerifier.from_frozen_runtime_manifest(
                    raw,
                    expected_manifest_digest=expected,
                    observed_policy_digest=policy.observed_digest,
                    observed_config_digest=config.observed_digest,
                    public_key=self._runtime_public_key,
                    now=current,
                    replay_guard=replay_guard,
                )
            except Exception as exc:
                return getattr(exc, "code", "memory_runtime_generation_invalid")

            self._verifier = verifier
            self._runtime_manifest_digest = expected
            self._runtime_expires_at = runtime_expires_at
            with self._cache_lock:
                self._cached_snapshot_digest = ""
                self._cached_decision = None
                self._cached_valid_until = None
        return None

    def _snapshot_decision(self, *, now: datetime | None = None) -> MemoryCapabilityDecision:
        current = _safe_now(now)
        if self._bootstrap_failure:
            return _local_denial(self._bootstrap_failure)
        refresh_failure = self._refresh_runtime_generation(current)
        if refresh_failure is not None:
            return _local_denial(refresh_failure)
        if self._verifier is None or self._snapshot_path is None or self._runtime_expires_at is None:
            return _local_denial("memory_runtime_unavailable")

        with self._cache_lock:
            try:
                _path, raw = _read_regular_file(
                    str(self._snapshot_path),
                    code="memory_snapshot_unavailable",
                    maximum_bytes=_MAX_SNAPSHOT_BYTES,
                )
            except Exception as exc:
                return _local_denial(getattr(exc, "code", "memory_snapshot_unavailable"))
            digest = hashlib.sha256(raw).hexdigest()
            if (
                digest == self._cached_snapshot_digest
                and self._cached_decision is not None
                and self._cached_decision.memory_allowed
                and self._cached_valid_until is not None
            ):
                if current < self._cached_valid_until:
                    return self._cached_decision
                return _local_denial("memory_snapshot_expired")

            decision = self._verifier.verify(raw, now=current)
            if decision.memory_allowed and decision.snapshot is not None:
                self._cached_snapshot_digest = digest
                self._cached_decision = decision
                self._cached_valid_until = min(
                    decision.snapshot.expires_at,
                    self._runtime_expires_at,
                )
            return decision

    def authorize_explicit_read(
        self,
        *,
        memory_provenance: MemoryTurnProvenance | None,
        tool_call_id: str,
        now: datetime | None = None,
    ) -> MemoryReadAuthorization:
        provider_name = self.provider_name
        if not self.active:
            return MemoryReadAuthorization(False, "memory_runtime_unconfigured", provider_name)
        if self._bootstrap_failure or self._config is None:
            return MemoryReadAuthorization(
                False,
                self._bootstrap_failure or "memory_runtime_unavailable",
                provider_name,
            )
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or len(tool_call_id) > 256
            or not validate_memory_turn_provenance(memory_provenance)
            or memory_provenance is None
            or not memory_provenance.authorizes_private_memory
        ):
            return MemoryReadAuthorization(False, "memory_provenance_denied", provider_name)
        current = _safe_now(now)
        try:
            authenticated_at = _parse_time(
                memory_provenance.authenticated_at,
                code="memory_provenance_denied",
            )
        except MemoryRuntimeError:
            return MemoryReadAuthorization(False, "memory_provenance_denied", provider_name)
        age_seconds = (current - authenticated_at).total_seconds()
        if (
            age_seconds > _MAX_PROVENANCE_AGE_SECONDS
            or age_seconds < -_MAX_PROVENANCE_FUTURE_SKEW_SECONDS
        ):
            return MemoryReadAuthorization(False, "memory_provenance_stale", provider_name)
        if memory_provenance.subject_id != self._config.subject_id:
            return MemoryReadAuthorization(False, "memory_subject_denied", provider_name)
        if memory_provenance.origin not in self._config.allowed_origins:
            return MemoryReadAuthorization(False, "memory_origin_denied", provider_name)
        try:
            if self._subject_bindings_path is None:
                raise MemoryRuntimeError(
                    "unsafe_subject_bindings_file", "memory subject bindings are unavailable"
                )
            current_bindings = load_memory_subject_bindings(
                self._subject_bindings_path
            )
            if current_bindings.content_digest != self._config.subject_bindings_digest:
                raise MemoryRuntimeError(
                    "subject_bindings_digest_mismatch", "memory subject bindings changed"
                )
        except Exception as exc:
            code = getattr(exc, "code", "unsafe_subject_bindings_file")
            return MemoryReadAuthorization(
                False,
                code if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code) else "unsafe_subject_bindings_file",
                provider_name,
            )
        decision = self._snapshot_decision(now=current)
        if not decision.memory_allowed:
            return MemoryReadAuthorization(
                False,
                decision.failure_code or "memory_capability_denied",
                provider_name,
            )
        return MemoryReadAuthorization(True, "memory_read_allowed", provider_name)


__all__ = [
    "CONFIG_ENV",
    "CONFIG_SCHEMA_VERSION",
    "DEPLOYMENT_MODE",
    "MemoryReadAuthorization",
    "MemoryRuntimeController",
    "MemoryRuntimeError",
    "POLICY_SCHEMA_VERSION",
    "PUBLIC_KEY_ENV",
    "REPLAY_STATE_ENV",
    "RUNTIME_MANIFEST_DIGEST_ENV",
    "RUNTIME_MANIFEST_ENV",
    "SNAPSHOT_ENV",
    "SUBJECT_BINDINGS_ENV",
]
