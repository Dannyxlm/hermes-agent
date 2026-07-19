"""Offline validation for Hermes' vendored CloudSeed memory protocol.

CloudSeed owns the protocol bundle.  Hermes consumes only the exact exported
bytes pinned below and a digest-bound, non-authorizing runtime manifest.  This
module deliberately has no provider, database, gateway, or network imports.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


PROTOCOL_BUNDLE_SCHEMA_VERSION = "memory-protocol-bundle/v1"
RUNTIME_MANIFEST_SCHEMA_VERSION = "memory-runtime-manifest/v3"
PROTOCOL_SOURCE_COMMIT = "a588d2ccd5a0354ba8b24ee4697ceee159dac4e7"
EXPECTED_PROTOCOL_BUNDLE_DIGEST = (
    "84ddd5ec305341d053c6f3cf15be0be85fce7b877f149018288979b52fc12bb4"
)
MAX_JSON_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 20_000
MAX_JSON_INTEGER = (1 << 63) - 1
RUNTIME_MAX_LIFETIME = timedelta(minutes=15)
RUNTIME_MAX_FUTURE_SKEW = timedelta(seconds=60)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VENDORED_PROTOCOL_ROOT = _REPOSITORY_ROOT / "contracts" / "cloudseed-memory" / "v1"

_SCHEMA_FILES = (
    "capability-snapshot.schema.json",
    "capability-envelope.schema.json",
    "denial.schema.json",
    "error.schema.json",
    "observation.schema.json",
    "policy-decision.schema.json",
    "provenance.schema.json",
    "receipt.schema.json",
)

# Pin every exported byte, including manifest whitespace.  The bundle digest
# authenticates schema payloads semantically; this table additionally makes a
# repackaged or hand-edited vendored tree fail deterministically.
_EXPECTED_FILE_SHA256 = {
    "capability-envelope.schema.json": "789693b644557d4fc666b33a949011ed26fd5f1734a65f3d0463da194030c89e",
    "capability-snapshot.schema.json": "db426e8c1048c7026c91c8b1d7dea99216ee589287c33ee561d6ec64e7afd528",
    "denial.schema.json": "4997595e71fdabba1d7c7f400da3b741511a1b5ba69e40fb991bdee6929eb157",
    "error.schema.json": "9263bd087b3ee3377d6c0631e612d0baeb7e87b682db2a5461dc899af3c46db3",
    "manifest.json": "df9f4efdbab558b1ea5189c3ad0c8ecaeeb8c6c4df75a420bbf1809815ebf1e4",
    "observation.schema.json": "a1ceebd95951231fadad1c537282d1c787e2e893690fb9251c65eae9e5dc366c",
    "policy-decision.schema.json": "eb1832ad79dec9a475ee2a7779001f8edce700d6516379960aa8303986ce9e14",
    "provenance.schema.json": "de5118ec6fb1a00950352127567d51e65b307c9071a9bb3a01330f427917b672",
    "receipt.schema.json": "675fe3ded8fd61902f378af8175b86adb4d68b10989c20f46e0efc548b17573e",
}

_RUNTIME_FIELDS = {
    "schema_version",
    "manifest_id",
    "generated_at",
    "expires_at",
    "plan",
    "release",
    "protocol",
    "policy",
    "config",
    "target",
    "sources",
    "wrappers",
    "schedulers",
    "wiki",
    "qdrant",
    "honcho",
    "built_in_memory",
    "services",
    "recovery",
    "units",
}


class MemoryProtocolError(ValueError):
    """A content-safe protocol/bootstrap failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MemoryProtocolBundle:
    bundle_digest: str
    protocol_version: int
    minimum_protocol_version: int
    policy_version: int
    minimum_policy_version: int
    capability_version: int
    minimum_capability_version: int
    security_epoch: int
    deployment_modes: tuple[str, ...]


@dataclass(frozen=True)
class MemoryRuntimeBinding:
    """Authenticated values Hermes is permitted to use for one release."""

    manifest_digest: str
    config_digest: str
    policy_digest: str
    protocol_bundle_digest: str
    protocol_version: int
    supported_protocol_versions: tuple[int, ...]
    minimum_protocol_version: int
    policy_version: int
    supported_policy_versions: tuple[int, ...]
    minimum_policy_version: int
    capability_version: int
    supported_capability_versions: tuple[int, ...]
    minimum_capability_version: int
    security_epoch: int
    deployment_mode: str
    destination: str
    audience: str
    capability_issuer: str
    capability_key_id: str
    capability_public_key_digest: str
    generated_at: datetime
    expires_at: datetime

    def require_fresh(self, now: datetime) -> None:
        current = _aware_utc(now, "now")
        if current + RUNTIME_MAX_FUTURE_SKEW < self.generated_at:
            raise MemoryProtocolError(
                "runtime_not_yet_valid",
                "the frozen memory runtime manifest is not yet valid",
            )
        if current >= self.expires_at:
            raise MemoryProtocolError(
                "runtime_expired",
                "the frozen memory runtime manifest has expired",
            )


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MemoryProtocolError("invalid_json", "value is not canonical JSON") from exc


def canonical_json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _bounded_utf8_size(value: str, *, remaining: int, context: str) -> int:
    if value.isascii():
        size = len(value)
        if size > remaining:
            raise MemoryProtocolError("invalid_json", f"{context} exceeds the safe text bound")
        return size
    size = 0
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise MemoryProtocolError("invalid_json", f"{context} contains invalid Unicode")
        if codepoint <= 0x7F:
            size += 1
        elif codepoint <= 0x7FF:
            size += 2
        elif codepoint <= 0xFFFF:
            size += 3
        else:
            size += 4
        if size > remaining:
            raise MemoryProtocolError("invalid_json", f"{context} exceeds the safe text bound")
    return size


def _preflight_json_primitives(
    value: Any,
    *,
    context: str,
    maximum_text_bytes: int,
) -> None:
    """Bound exact in-memory JSON primitives before any serialization work."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    nodes = 0
    text_bytes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise MemoryProtocolError(
                "invalid_json",
                f"{context} exceeds the safe structure bound",
            )
        current_type = type(current)
        if current_type is dict:
            identity = id(current)
            if identity in seen_containers:
                raise MemoryProtocolError(
                    "invalid_json",
                    f"{context} contains a repeated or cyclic container",
                )
            seen_containers.add(identity)
            if nodes + len(stack) + (2 * len(current)) > MAX_JSON_NODES:
                raise MemoryProtocolError(
                    "invalid_json",
                    f"{context} exceeds the safe node bound",
                )
            nodes += len(current)  # Object keys count toward the node budget.
            for key, item in current.items():
                if type(key) is not str:
                    raise MemoryProtocolError(
                        "invalid_json",
                        f"{context} object keys must be exact strings",
                    )
                text_bytes += _bounded_utf8_size(
                    key,
                    remaining=maximum_text_bytes - text_bytes,
                    context=context,
                )
                stack.append((item, depth + 1))
        elif current_type is list:
            identity = id(current)
            if identity in seen_containers:
                raise MemoryProtocolError(
                    "invalid_json",
                    f"{context} contains a repeated or cyclic container",
                )
            seen_containers.add(identity)
            if nodes + len(stack) + len(current) > MAX_JSON_NODES:
                raise MemoryProtocolError(
                    "invalid_json",
                    f"{context} exceeds the safe node bound",
                )
            for item in current:
                stack.append((item, depth + 1))
        elif current_type is str:
            text_bytes += _bounded_utf8_size(
                current,
                remaining=maximum_text_bytes - text_bytes,
                context=context,
            )
        elif current is None or current_type is bool:
            continue
        elif current_type is int:
            if abs(current) > MAX_JSON_INTEGER:
                raise MemoryProtocolError(
                    "invalid_json",
                    f"{context} contains an out-of-bounds integer",
                )
        elif current_type is float:
            if not math.isfinite(current):
                raise MemoryProtocolError(
                    "invalid_json",
                    f"{context} contains a non-JSON number",
                )
        else:
            raise MemoryProtocolError(
                "invalid_json",
                f"{context} contains a non-primitive JSON value",
            )


def strict_json_loads(
    value: bytes | str | Mapping[str, Any],
    *,
    context: str,
    maximum_bytes: int = MAX_JSON_BYTES,
) -> dict[str, Any]:
    """Parse a bounded JSON object while rejecting duplicate keys and NaN."""

    value_type = type(value)
    if value_type is dict:
        # Round-tripping also rejects non-JSON values and prevents callers from
        # retaining mutable nested objects inside a validated result.
        _preflight_json_primitives(
            value,
            context=context,
            maximum_text_bytes=maximum_bytes,
        )
        encoded = canonical_json_bytes(value)
        if len(encoded) > maximum_bytes:
            raise MemoryProtocolError("invalid_json", f"{context} exceeds the safe byte bound")
        value = encoded
    elif value_type is str:
        _bounded_utf8_size(value, remaining=maximum_bytes, context=context)
        try:
            value = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise MemoryProtocolError("invalid_json", f"{context} is not UTF-8 JSON") from exc
    elif value_type is not bytes:
        raise MemoryProtocolError(
            "invalid_json",
            f"{context} must use exact bytes, string, or dictionary primitives",
        )
    if len(value) > maximum_bytes:
        raise MemoryProtocolError("invalid_json", f"{context} must be bounded UTF-8 JSON")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemoryProtocolError("invalid_json", f"{context} is not UTF-8 JSON") from exc

    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise MemoryProtocolError(
                    "duplicate_json_key",
                    f"{context} contains a duplicate key",
                )
            result[key] = item
        return result

    def reject_constant(_constant: str) -> Any:
        raise MemoryProtocolError("invalid_json", f"{context} contains a non-JSON number")

    def parse_integer(raw: str) -> int:
        if len(raw.lstrip("-")) > 19:
            raise MemoryProtocolError(
                "invalid_json",
                f"{context} contains an out-of-bounds integer",
            )
        parsed = int(raw)
        if abs(parsed) > MAX_JSON_INTEGER:
            raise MemoryProtocolError(
                "invalid_json",
                f"{context} contains an out-of-bounds integer",
            )
        return parsed

    def parse_float_number(raw: str) -> float:
        if len(raw) > 64:
            raise MemoryProtocolError(
                "invalid_json",
                f"{context} contains an out-of-bounds number",
            )
        parsed = float(raw)
        if not math.isfinite(parsed):
            raise MemoryProtocolError(
                "invalid_json",
                f"{context} contains a non-JSON number",
            )
        return parsed

    try:
        payload = json.loads(
            text,
            object_pairs_hook=object_hook,
            parse_constant=reject_constant,
            parse_int=parse_integer,
            parse_float=parse_float_number,
        )
    except MemoryProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise MemoryProtocolError("invalid_json", f"{context} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise MemoryProtocolError("schema_mismatch", f"{context} must be an object")
    _preflight_json_primitives(
        payload,
        context=context,
        maximum_text_bytes=maximum_bytes,
    )
    return payload


def _read_regular_file(path: Path, *, context: str) -> bytes:
    nonblock = getattr(os, "O_NONBLOCK", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nonblock is None or nofollow is None:
        raise MemoryProtocolError(
            "protocol_bundle_invalid",
            "nonblocking protocol file validation is unavailable",
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
        | nonblock
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MemoryProtocolError(
            "protocol_bundle_invalid",
            f"{context} must be a readable regular non-symlink file",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MemoryProtocolError(
                "protocol_bundle_invalid",
                f"{context} must be a regular file",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise MemoryProtocolError(
                "protocol_bundle_invalid",
                f"{context} exceeds the safe byte bound",
            )
        return raw
    finally:
        os.close(descriptor)


def _exact_object(value: Any, fields: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MemoryProtocolError("schema_mismatch", f"{context} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise MemoryProtocolError("schema_mismatch", f"{context} fields do not match the contract")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MemoryProtocolError("schema_mismatch", f"{field} must be a positive integer")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise MemoryProtocolError("schema_mismatch", f"{field} must be a lowercase sha256 digest")
    return value


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
    ):
        raise MemoryProtocolError("schema_mismatch", f"{field} must be bounded non-empty text")
    return value


def _closed_supported_range(
    protocol: Mapping[str, Any],
    *,
    current_field: str,
    minimum_field: str,
    supported_field: str,
    label: str,
    implemented_minimum: int,
    implemented_maximum: int,
) -> tuple[int, int, tuple[int, ...]]:
    current = _positive_integer(protocol[current_field], f"protocol.{current_field}")
    minimum = _positive_integer(protocol[minimum_field], f"protocol.{minimum_field}")
    if (
        minimum < implemented_minimum
        or current > implemented_maximum
        or minimum > current
    ):
        raise MemoryProtocolError(
            "runtime_protocol_range_mismatch",
            f"runtime {label} support exceeds the exact vendored bundle range",
        )
    supported = protocol[supported_field]
    expected_length = current - minimum + 1
    if (
        not isinstance(supported, list)
        or len(supported) != expected_length
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value != minimum + offset
            for offset, value in enumerate(supported)
        )
    ):
        raise MemoryProtocolError(
            "runtime_protocol_invalid",
            f"runtime {label} support is not a closed version range",
        )
    return current, minimum, tuple(supported)


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise MemoryProtocolError("schema_mismatch", f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryProtocolError("schema_mismatch", f"{field} must be an RFC3339 timestamp") from exc
    if parsed.utcoffset() is None:
        raise MemoryProtocolError("schema_mismatch", f"{field} must include a timezone")
    offset = parsed.utcoffset()
    if offset is None or abs(offset) >= timedelta(hours=24):
        raise MemoryProtocolError("schema_mismatch", f"{field} has an invalid timezone offset")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MemoryProtocolError("schema_mismatch", f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def verify_vendored_protocol_bundle(
    root: Path | str = VENDORED_PROTOCOL_ROOT,
) -> MemoryProtocolBundle:
    """Verify the exported bytes, manifest checksums, and fixed bundle digest."""

    directory = Path(root)
    if directory.is_symlink() or not directory.is_dir():
        raise MemoryProtocolError(
            "protocol_bundle_invalid",
            "vendored memory protocol root must be a regular non-symlink directory",
        )
    actual_names = {path.name for path in directory.iterdir()}
    expected_names = set(_EXPECTED_FILE_SHA256)
    if actual_names != expected_names:
        raise MemoryProtocolError(
            "protocol_bundle_invalid",
            "vendored memory protocol contains missing or unexpected files",
        )

    raw_files: dict[str, bytes] = {}
    for name, expected_digest in _EXPECTED_FILE_SHA256.items():
        raw = _read_regular_file(directory / name, context=f"protocol file {name}")
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise MemoryProtocolError(
                "protocol_bundle_invalid",
                f"vendored memory protocol byte drift: {name}",
            )
        raw_files[name] = raw

    manifest = strict_json_loads(raw_files["manifest.json"], context="protocol manifest")
    _exact_object(
        manifest,
        {
            "schema_version",
            "protocol_version",
            "minimum_protocol_version",
            "policy_version",
            "minimum_policy_version",
            "capability_version",
            "minimum_capability_version",
            "security_epoch",
            "deployment_modes",
            "files",
            "bundle_digest",
        },
        "protocol manifest",
    )
    if manifest["schema_version"] != PROTOCOL_BUNDLE_SCHEMA_VERSION:
        raise MemoryProtocolError("protocol_bundle_invalid", "unsupported protocol bundle schema")

    manifest_files = []
    for name in _SCHEMA_FILES:
        schema = strict_json_loads(raw_files[name], context=f"protocol schema {name}")
        if schema.get("additionalProperties") is not False:
            raise MemoryProtocolError(
                "protocol_bundle_invalid",
                f"protocol schema is not closed-world: {name}",
            )
        manifest_files.append(
            {"path": name, "sha256": hashlib.sha256(raw_files[name]).hexdigest()}
        )

    material = {
        "schema_version": manifest["schema_version"],
        "protocol_version": manifest["protocol_version"],
        "minimum_protocol_version": manifest["minimum_protocol_version"],
        "policy_version": manifest["policy_version"],
        "minimum_policy_version": manifest["minimum_policy_version"],
        "capability_version": manifest["capability_version"],
        "minimum_capability_version": manifest["minimum_capability_version"],
        "security_epoch": manifest["security_epoch"],
        "deployment_modes": manifest["deployment_modes"],
        "files": manifest_files,
    }
    computed_digest = canonical_json_digest(material)
    if (
        manifest["files"] != manifest_files
        or manifest["bundle_digest"] != computed_digest
        or computed_digest != EXPECTED_PROTOCOL_BUNDLE_DIGEST
    ):
        raise MemoryProtocolError(
            "protocol_bundle_invalid",
            "vendored memory protocol manifest or digest drift",
        )

    protocol_version = _positive_integer(manifest["protocol_version"], "protocol_version")
    minimum_protocol_version = _positive_integer(
        manifest["minimum_protocol_version"], "minimum_protocol_version"
    )
    policy_version = _positive_integer(manifest["policy_version"], "policy_version")
    minimum_policy_version = _positive_integer(
        manifest["minimum_policy_version"], "minimum_policy_version"
    )
    capability_version = _positive_integer(
        manifest["capability_version"], "capability_version"
    )
    minimum_capability_version = _positive_integer(
        manifest["minimum_capability_version"], "minimum_capability_version"
    )
    security_epoch = _positive_integer(manifest["security_epoch"], "security_epoch")
    if minimum_protocol_version > protocol_version:
        raise MemoryProtocolError("protocol_bundle_invalid", "protocol version range is empty")
    if minimum_policy_version > policy_version:
        raise MemoryProtocolError("protocol_bundle_invalid", "policy version range is empty")
    if minimum_capability_version > capability_version:
        raise MemoryProtocolError("protocol_bundle_invalid", "capability version range is empty")
    modes = manifest["deployment_modes"]
    if (
        not isinstance(modes, list)
        or not modes
        or any(not isinstance(mode, str) or not mode for mode in modes)
        or len(set(modes)) != len(modes)
    ):
        raise MemoryProtocolError("protocol_bundle_invalid", "deployment mode set is invalid")
    return MemoryProtocolBundle(
        bundle_digest=computed_digest,
        protocol_version=protocol_version,
        minimum_protocol_version=minimum_protocol_version,
        policy_version=policy_version,
        minimum_policy_version=minimum_policy_version,
        capability_version=capability_version,
        minimum_capability_version=minimum_capability_version,
        security_epoch=security_epoch,
        deployment_modes=tuple(modes),
    )


def load_memory_runtime_binding(
    runtime_manifest: bytes | str | Mapping[str, Any],
    *,
    expected_manifest_digest: str,
    observed_policy_digest: str,
    observed_config_digest: str,
    now: datetime,
    protocol_root: Path | str = VENDORED_PROTOCOL_ROOT,
) -> MemoryRuntimeBinding:
    """Bind Hermes to one authenticated freeze and current policy/config bytes.

    ``expected_manifest_digest`` comes from the managed release configuration;
    ``observed_*_digest`` values are computed by the caller from the local files
    it actually loaded.  The snapshot is therefore unable to switch policy,
    configuration, target, or key identity by merely claiming new values.
    """

    _sha256(expected_manifest_digest, "expected_manifest_digest")
    _sha256(observed_policy_digest, "observed_policy_digest")
    _sha256(observed_config_digest, "observed_config_digest")
    bundle = verify_vendored_protocol_bundle(protocol_root)
    manifest = strict_json_loads(runtime_manifest, context="memory runtime manifest")
    _exact_object(manifest, _RUNTIME_FIELDS, "memory runtime manifest")
    actual_manifest_digest = canonical_json_digest(manifest)
    if actual_manifest_digest != expected_manifest_digest:
        raise MemoryProtocolError(
            "runtime_manifest_digest_mismatch",
            "memory runtime manifest does not match the managed release digest",
        )
    if manifest["schema_version"] != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise MemoryProtocolError("runtime_schema_mismatch", "unsupported memory runtime manifest")
    _bounded_text(manifest["manifest_id"], "manifest_id", 120)
    generated_at = _parse_time(manifest["generated_at"], "generated_at")
    expires_at = _parse_time(manifest["expires_at"], "expires_at")
    if expires_at <= generated_at or expires_at - generated_at > RUNTIME_MAX_LIFETIME:
        raise MemoryProtocolError(
            "runtime_lifetime_invalid",
            "memory runtime manifest lifetime is invalid",
        )

    release = _exact_object(
        manifest["release"],
        {"generation", "deployment_mode", "authorization"},
        "runtime release",
    )
    if release["generation"] != "memory-v3-readonly-core" or release["authorization"] != "none":
        raise MemoryProtocolError(
            "runtime_authorization_invalid",
            "runtime manifest is not the non-authorizing read-only generation",
        )
    deployment_mode = release["deployment_mode"]
    if deployment_mode not in bundle.deployment_modes:
        raise MemoryProtocolError("deployment_mode_mismatch", "unsupported runtime deployment mode")

    protocol = _exact_object(
        manifest["protocol"],
        {
            "bundle_digest",
            "protocol_version",
            "supported_versions",
            "minimum_version",
            "policy_version",
            "supported_policy_versions",
            "minimum_policy_version",
            "capability_version",
            "supported_capability_versions",
            "minimum_capability_version",
            "security_epoch",
        },
        "runtime protocol",
    )
    if protocol["bundle_digest"] != bundle.bundle_digest:
        raise MemoryProtocolError(
            "protocol_bundle_digest_mismatch",
            "runtime manifest names a different protocol bundle",
        )
    protocol_version, minimum_protocol, supported_protocol = _closed_supported_range(
        protocol,
        current_field="protocol_version",
        minimum_field="minimum_version",
        supported_field="supported_versions",
        label="protocol",
        implemented_minimum=bundle.minimum_protocol_version,
        implemented_maximum=bundle.protocol_version,
    )
    policy_version, minimum_policy, supported_policy = _closed_supported_range(
        protocol,
        current_field="policy_version",
        minimum_field="minimum_policy_version",
        supported_field="supported_policy_versions",
        label="policy",
        implemented_minimum=bundle.minimum_policy_version,
        implemented_maximum=bundle.policy_version,
    )
    capability_version, minimum_capability, supported_capability = _closed_supported_range(
        protocol,
        current_field="capability_version",
        minimum_field="minimum_capability_version",
        supported_field="supported_capability_versions",
        label="capability",
        implemented_minimum=bundle.minimum_capability_version,
        implemented_maximum=bundle.capability_version,
    )
    security_epoch = _positive_integer(protocol["security_epoch"], "protocol.security_epoch")
    if security_epoch != bundle.security_epoch:
        raise MemoryProtocolError(
            "runtime_security_epoch_mismatch",
            "runtime security epoch does not match the frozen vendored bundle",
        )

    policy = _exact_object(manifest["policy"], {"digest", "write_mode"}, "runtime policy")
    policy_digest = _sha256(policy["digest"], "policy.digest")
    if policy["write_mode"] != "deny" or policy_digest != observed_policy_digest:
        raise MemoryProtocolError(
            "policy_digest_mismatch",
            "runtime policy does not match the loaded deny policy",
        )
    config = _exact_object(manifest["config"], {"present", "digest"}, "runtime config")
    config_digest = _sha256(config["digest"], "config.digest")
    if config["present"] is not True or config_digest != observed_config_digest:
        raise MemoryProtocolError(
            "config_digest_mismatch",
            "runtime configuration does not match the loaded configuration",
        )

    target = _exact_object(
        manifest["target"],
        {
            "destination",
            "audience",
            "capability_issuer",
            "capability_key_id",
            "capability_public_key_digest",
            "status_connection_env",
            "access_precondition",
        },
        "runtime target",
    )
    binding = MemoryRuntimeBinding(
        manifest_digest=actual_manifest_digest,
        config_digest=config_digest,
        policy_digest=policy_digest,
        protocol_bundle_digest=bundle.bundle_digest,
        protocol_version=protocol_version,
        supported_protocol_versions=supported_protocol,
        minimum_protocol_version=minimum_protocol,
        policy_version=policy_version,
        supported_policy_versions=supported_policy,
        minimum_policy_version=minimum_policy,
        capability_version=capability_version,
        supported_capability_versions=supported_capability,
        minimum_capability_version=minimum_capability,
        security_epoch=security_epoch,
        deployment_mode=deployment_mode,
        destination=_bounded_text(target["destination"], "target.destination", 160),
        audience=_bounded_text(target["audience"], "target.audience", 160),
        capability_issuer=_bounded_text(
            target["capability_issuer"], "target.capability_issuer", 120
        ),
        capability_key_id=_bounded_text(
            target["capability_key_id"], "target.capability_key_id", 120
        ),
        capability_public_key_digest=_sha256(
            target["capability_public_key_digest"],
            "target.capability_public_key_digest",
        ),
        generated_at=generated_at,
        expires_at=expires_at,
    )
    binding.require_fresh(now)
    return binding


__all__ = [
    "EXPECTED_PROTOCOL_BUNDLE_DIGEST",
    "MemoryProtocolBundle",
    "MemoryProtocolError",
    "MemoryRuntimeBinding",
    "PROTOCOL_SOURCE_COMMIT",
    "VENDORED_PROTOCOL_ROOT",
    "canonical_json_bytes",
    "canonical_json_digest",
    "load_memory_runtime_binding",
    "strict_json_loads",
    "verify_vendored_protocol_bundle",
]
