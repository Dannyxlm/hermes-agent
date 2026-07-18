"""Offline, fail-closed Honcho SDK capability probing.

This module deliberately knows nothing about the Honcho client, session manager,
HTTP transport, credentials, or message contents.  A caller supplies the SDK
metadata plus small fake/test callables and readbacks.  The probe only verifies
that the named assumptions still have the shape that the provider relies on;
it never discovers capabilities by making a network request.

The returned dictionary is a content-free ``honcho-capability-receipt-v1``:
readback values are used for validation but are never copied into the receipt.
That makes the receipt safe to persist or attach to an operational audit event.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plugins.memory.honcho.bounds import run_bounded


RECEIPT_SCHEMA = "honcho-capability-receipt-v1"
SUPPORTED = "supported"
UNSUPPORTED_API = "unsupported_api"

REQUIRED_CAPABILITIES = frozenset({
    "peer_handle",
    "session_handle",
    "session_add_peers_readback",
    "message_add",
    "peer_card_read",
    "human_message_search",
    "peer_context",
    "conclusion_list",
    "conclusion_query",
    "conclusion_create",
    "reasoning_chat",
})

_ACCEPTED_STATUSES = frozenset({"accepted", "passed", "supported"})
_KNOWN_FAILURE_STATUSES = frozenset(
    {"rejected", "ignored", "missing", "unsupported_api"}
)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """One named behavior assumed by the Honcho provider.

    ``callable_name`` resolves in
    :class:`HonchoCapabilityProbeDependencies.callables`.  ``readback_name``
    resolves in its ``readbacks`` mapping.  A check may instead carry the
    callables directly through ``operation`` and ``readback``; this is useful
    for tiny unit-test fixtures and keeps the probe dependency-injected.

    A successful operation must return an envelope with ``status=accepted``
    (or ``accepted=True``).  Its effective readback must match either
    ``expected_readback`` exactly or ``expected_shape`` structurally.  A
    missing expectation is itself unsupported: a probe without a contract is
    not evidence of compatibility.
    """

    name: str
    callable_name: str | None = None
    readback_name: str | None = None
    expected_shape: Any = None
    expected_readback: Any = _MISSING
    paid: bool = False
    operation: Callable[[], Any] | None = None
    readback: Callable[[], Any] | None = None


@dataclass(slots=True)
class HonchoCapabilityProbeDependencies:
    """All runtime inputs required by the offline probe.

    The mappings are intentionally injected.  The probe never imports the
    Honcho SDK or provider implementation and therefore cannot accidentally
    turn a capability check into a live API call.
    """

    sdk_version: str = ""
    sdk_source_hash: str = ""
    provider_source_hash: str = ""
    probe_manifest_hash: str = ""
    callables: Mapping[str, Callable[[], Any]] = field(default_factory=dict)
    readbacks: Mapping[str, Any] = field(default_factory=dict)


# A public alias is useful to callers that call the values "operations" rather
# than "callables".  Keep the canonical class name explicit in the receipt API.
CapabilityProbeDependencies = HonchoCapabilityProbeDependencies


def _canonical_json(value: Any) -> str:
    """Serialize manifest data deterministically without accepting code."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Return the deterministic SHA-256 binding for a probe manifest."""

    return "sha256:" + hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping_value(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return _MISSING


def _is_paid_chat(name: str, paid: bool) -> bool:
    if paid:
        return True
    normalized = name.strip().lower().replace("-", "_")
    return normalized in {"chat", "peer_chat", "reasoning_chat", "dialectic_chat"}


def _approved_paid_check(approved_manifest: Any, name: str) -> bool:
    """Return whether *name* was explicitly approved for a paid probe."""

    if approved_manifest is True:
        return True
    if not isinstance(approved_manifest, Mapping):
        return False
    if approved_manifest.get("approved") is False:
        return False
    names = _mapping_value(
        approved_manifest,
        "paid_checks",
        "approved_paid_checks",
        "checks",
    )
    return isinstance(names, Sequence) and not isinstance(names, (str, bytes)) and name in names


def _normalise_check(name: str, value: Any) -> CapabilityCheck | None:
    if isinstance(value, CapabilityCheck):
        if value.name != name:
            return CapabilityCheck(
                name=name,
                callable_name=value.callable_name,
                readback_name=value.readback_name,
                expected_shape=value.expected_shape,
                expected_readback=value.expected_readback,
                paid=value.paid,
                operation=value.operation,
                readback=value.readback,
            )
        return value
    if not isinstance(value, Mapping):
        return None

    operation = _mapping_value(value, "operation", "invoke", "probe", "callable")
    callable_name = operation if isinstance(operation, str) else None
    direct_operation = operation if callable(operation) else None
    readback = _mapping_value(value, "readback")
    readback_name = readback if isinstance(readback, str) else None
    direct_readback = readback if callable(readback) else None
    expected_readback = _mapping_value(value, "expected_readback", "expected", "expect")
    expected_shape = _mapping_value(value, "expected_shape", "readback_shape", "shape")
    if expected_shape is _MISSING:
        expected_shape = None
    if expected_readback is _MISSING:
        expected_readback = _MISSING

    return CapabilityCheck(
        name=name,
        callable_name=callable_name,
        readback_name=readback_name,
        expected_shape=expected_shape,
        expected_readback=expected_readback,
        paid=value.get("paid") is True,
        operation=direct_operation,
        readback=direct_readback,
    )


def _normalise_checks(checks: Any) -> tuple[CapabilityCheck, ...] | None:
    if isinstance(checks, Mapping):
        result: list[CapabilityCheck] = []
        for raw_name, value in checks.items():
            name = _text(raw_name)
            if not name:
                return None
            check = _normalise_check(name, value)
            if check is None:
                return None
            result.append(check)
        return tuple(result)

    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        result = []
        seen: set[str] = set()
        for value in checks:
            if isinstance(value, CapabilityCheck):
                check = value
            elif isinstance(value, Mapping):
                raw_name = value.get("name")
                name = _text(raw_name)
                check = _normalise_check(name, value) if name else None
            else:
                check = None
            if check is None or not _text(check.name) or check.name in seen:
                return None
            seen.add(check.name)
            result.append(check)
        return tuple(result)

    return None


def _resolve_operation(
    check: CapabilityCheck,
    dependencies: HonchoCapabilityProbeDependencies,
) -> Callable[[], Any] | None:
    if callable(check.operation):
        return check.operation
    name = check.callable_name or check.name
    candidate = dependencies.callables.get(name)
    return candidate if callable(candidate) else None


def _resolve_readback(
    check: CapabilityCheck,
    dependencies: HonchoCapabilityProbeDependencies,
) -> Any:
    if callable(check.readback):
        return check.readback
    name = check.readback_name or check.name
    return dependencies.readbacks.get(name, _MISSING)


def _invoke(value: Any, *, timeout_seconds: float = 2.0) -> tuple[bool, Any]:
    if not callable(value):
        return True, value
    outcome = run_bounded(value, timeout_seconds=timeout_seconds)
    if outcome.status != "ok":
        return False, _MISSING
    return True, outcome.value


def _operation_status(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "shape_drift"
    if "status" in value:
        status = value["status"]
        if status in _ACCEPTED_STATUSES:
            return "accepted"
        if isinstance(status, str) and status in _KNOWN_FAILURE_STATUSES:
            return status
        return "shape_drift"
    if "accepted" in value:
        return "accepted" if value["accepted"] is True else "rejected"
    if "decision" in value:
        decision = value["decision"]
        if decision in _ACCEPTED_STATUSES:
            return "accepted"
        if isinstance(decision, str) and decision in _KNOWN_FAILURE_STATUSES:
            return decision
        return "shape_drift"
    return "missing"


def _shape_matches(value: Any, expected: Any) -> bool:
    """Match a JSON-like shape specification without retaining its values."""

    if expected is None or expected is _MISSING:
        return False
    if expected is Any:
        return True
    if isinstance(expected, type):
        return type(value) is expected
    if isinstance(expected, str):
        return {
            "mapping": isinstance(value, Mapping),
            "dict": isinstance(value, Mapping),
            "sequence": isinstance(value, Sequence) and not isinstance(value, (str, bytes)),
            "list": type(value) is list,
            "string": type(value) is str,
            "bool": type(value) is bool,
            "boolean": type(value) is bool,
            "int": type(value) is int,
            "float": type(value) is float,
            "none": value is None,
        }.get(expected, False)
    if isinstance(expected, Mapping):
        if not isinstance(value, Mapping) or set(value) != set(expected):
            return False
        return all(_shape_matches(value[key], shape) for key, shape in expected.items())
    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes)):
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != len(expected):
            return False
        return all(_shape_matches(actual, shape) for actual, shape in zip(value, expected))
    return value == expected


def _readback_from_operation(operation_result: Any) -> Any:
    if isinstance(operation_result, Mapping) and "readback" in operation_result:
        return operation_result["readback"]
    return _MISSING


def _failure_receipt(
    *,
    sdk_version: str,
    sdk_source_hash: str,
    provider_source_hash: str,
    probe_manifest_hash: str,
    checks: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "state": UNSUPPORTED_API,
        "sdk_version": sdk_version,
        "sdk_source_hash": sdk_source_hash,
        "provider_source_hash": provider_source_hash,
        "probe_manifest_hash": probe_manifest_hash,
        "checks": dict(checks),
    }


def probe_honcho_capabilities(
    dependencies: HonchoCapabilityProbeDependencies | None = None,
    manifest: Mapping[str, Any] | None = None,
    *,
    checks: Any = None,
    callables: Mapping[str, Callable[[], Any]] | None = None,
    readbacks: Mapping[str, Any] | None = None,
    sdk_version: str | None = None,
    sdk_source_hash: str | None = None,
    provider_source_hash: str | None = None,
    probe_manifest_hash: str | None = None,
    expected_sdk_version: str | None = None,
    expected_sdk_source_hash: str | None = None,
    expected_provider_source_hash: str | None = None,
    approved_test_manifest: Any = None,
) -> dict[str, Any]:
    """Run an isolated Honcho capability probe and return a receipt.

    All arguments are data or injected callables.  No SDK import and no network
    operation occurs here.  The function is intentionally conservative:
    malformed manifests, absent metadata, rejected/ignored/missing results,
    readback shape drift, and version/hash mismatches all produce
    ``state=unsupported_api``.
    """

    raw_manifest = manifest if isinstance(manifest, Mapping) else {}
    base = dependencies or HonchoCapabilityProbeDependencies()
    injected = HonchoCapabilityProbeDependencies(
        sdk_version=_text(sdk_version) or _text(base.sdk_version),
        sdk_source_hash=_text(sdk_source_hash) or _text(base.sdk_source_hash),
        provider_source_hash=_text(provider_source_hash) or _text(base.provider_source_hash),
        probe_manifest_hash=(
            _text(probe_manifest_hash)
            or _text(base.probe_manifest_hash)
            or _text(raw_manifest.get("probe_manifest_hash"))
            or _text(raw_manifest.get("manifest_hash"))
        ),
        callables=callables if callables is not None else base.callables,
        readbacks=readbacks if readbacks is not None else base.readbacks,
    )

    if checks is None:
        checks = raw_manifest.get("checks")
    normalized_checks = _normalise_checks(checks)
    if approved_test_manifest is None:
        approved_test_manifest = raw_manifest.get("approved_test_manifest")

    expected_version = (
        _text(expected_sdk_version)
        or _text(raw_manifest.get("expected_sdk_version"))
        or _text(raw_manifest.get("sdk_version"))
    )
    expected_sdk_hash = (
        _text(expected_sdk_source_hash)
        or _text(raw_manifest.get("expected_sdk_source_hash"))
    )
    expected_provider_hash = (
        _text(expected_provider_source_hash)
        or _text(raw_manifest.get("expected_provider_source_hash"))
    )

    metadata_missing = not all(
        (
            injected.sdk_version,
            injected.sdk_source_hash,
            injected.provider_source_hash,
            injected.probe_manifest_hash,
        )
    )
    version_mismatch = bool(expected_version and injected.sdk_version != expected_version)
    version_mismatch = version_mismatch or bool(
        expected_sdk_hash and injected.sdk_source_hash != expected_sdk_hash
    )
    version_mismatch = version_mismatch or bool(
        expected_provider_hash and injected.provider_source_hash != expected_provider_hash
    )

    if normalized_checks is None or not normalized_checks:
        reason = "missing" if normalized_checks is not None else "shape_drift"
        return _failure_receipt(
            sdk_version=injected.sdk_version,
            sdk_source_hash=injected.sdk_source_hash,
            provider_source_hash=injected.provider_source_hash,
            probe_manifest_hash=injected.probe_manifest_hash,
            checks={"manifest": {"state": UNSUPPORTED_API, "reason": reason}},
        )

    if metadata_missing or version_mismatch:
        reason = "version_mismatch" if version_mismatch else "missing"
        return _failure_receipt(
            sdk_version=injected.sdk_version,
            sdk_source_hash=injected.sdk_source_hash,
            provider_source_hash=injected.provider_source_hash,
            probe_manifest_hash=injected.probe_manifest_hash,
            checks={
                check.name: {"state": UNSUPPORTED_API, "reason": reason}
                for check in normalized_checks
            },
        )

    statuses: dict[str, Mapping[str, str]] = {}
    all_passed = True
    for check in normalized_checks:
        if _is_paid_chat(check.name, check.paid) and not _approved_paid_check(
            approved_test_manifest, check.name
        ):
            statuses[check.name] = {
                "state": UNSUPPORTED_API,
                "reason": "paid_chat_not_approved",
            }
            all_passed = False
            continue

        operation = _resolve_operation(check, injected)
        if operation is None:
            statuses[check.name] = {"state": UNSUPPORTED_API, "reason": "missing"}
            all_passed = False
            continue

        operation_ok, operation_result = _invoke(operation)
        if not operation_ok:
            statuses[check.name] = {"state": UNSUPPORTED_API, "reason": "rejected"}
            all_passed = False
            continue
        status = _operation_status(operation_result)
        if status != "accepted":
            statuses[check.name] = {"state": UNSUPPORTED_API, "reason": status}
            all_passed = False
            continue

        readback_source = _resolve_readback(check, injected)
        if readback_source is _MISSING:
            effective_readback = _readback_from_operation(operation_result)
        else:
            readback_ok, effective_readback = _invoke(readback_source)
            if not readback_ok:
                statuses[check.name] = {"state": UNSUPPORTED_API, "reason": "rejected"}
                all_passed = False
                continue

        if effective_readback is _MISSING:
            statuses[check.name] = {"state": UNSUPPORTED_API, "reason": "missing"}
            all_passed = False
            continue

        if check.expected_readback is not _MISSING:
            shape_ok = effective_readback == check.expected_readback
        else:
            shape_ok = _shape_matches(effective_readback, check.expected_shape)
        if not shape_ok:
            statuses[check.name] = {"state": UNSUPPORTED_API, "reason": "shape_drift"}
            all_passed = False
            continue

        statuses[check.name] = {"state": "passed", "readback": "effective"}

    state = SUPPORTED if all_passed and len(statuses) == len(normalized_checks) else UNSUPPORTED_API
    return {
        "schema": RECEIPT_SCHEMA,
        "state": state,
        "sdk_version": injected.sdk_version,
        "sdk_source_hash": injected.sdk_source_hash,
        "provider_source_hash": injected.provider_source_hash,
        "probe_manifest_hash": injected.probe_manifest_hash,
        "checks": statuses,
    }


# Keep names discoverable for callers that use the generic terminology.
probe_capabilities = probe_honcho_capabilities
run_capability_probe = probe_honcho_capabilities


def probe_required_honcho_capabilities(
    dependencies: HonchoCapabilityProbeDependencies,
    *,
    checks: Any,
    approved_test_manifest: Any = None,
) -> dict[str, Any]:
    """Probe the complete H2 assumption set or fail before invoking anything."""
    normalized = _normalise_checks(checks)
    names = {check.name for check in normalized or ()}
    missing = sorted(REQUIRED_CAPABILITIES - names)
    if normalized is None or missing:
        return _failure_receipt(
            sdk_version=dependencies.sdk_version,
            sdk_source_hash=dependencies.sdk_source_hash,
            provider_source_hash=dependencies.provider_source_hash,
            probe_manifest_hash=dependencies.probe_manifest_hash,
            checks={
                name: {"state": UNSUPPORTED_API, "reason": "missing"}
                for name in (missing or ["manifest"])
            },
        )
    return probe_honcho_capabilities(
        dependencies=dependencies,
        checks=normalized,
        approved_test_manifest=approved_test_manifest,
    )


def _sha256_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
        return text
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def build_public_capability_receipt(
    probe_receipt: Mapping[str, Any],
    *,
    config_revision_sha256: str,
    disposable_workspace_hmac: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Adapt the internal probe result to CloudSeed's public content-free contract."""
    generated = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    raw_checks = probe_receipt.get("checks")
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    paid_seen = False
    paid_approved = False
    if isinstance(raw_checks, Mapping):
        for name in sorted(raw_checks):
            detail = raw_checks.get(name)
            detail = detail if isinstance(detail, Mapping) else {}
            passed = detail.get("state") == "passed" and detail.get("readback") == "effective"
            checks.append({
                "capability": str(name),
                "status": "passed" if passed else "failed",
                "effective_readback": passed,
            })
            if _is_paid_chat(str(name), False):
                paid_seen = True
                paid_approved = passed
            if not passed:
                reason = str(detail.get("reason") or "shape_drift")
                error_map = {
                    "missing": "missing_callable",
                    "ignored": "ignored_parameter",
                    "shape_drift": "shape_drift",
                    "version_mismatch": "version_mismatch",
                    "paid_chat_not_approved": "unapproved_paid_probe",
                    "rejected": "exception",
                }
                code = error_map.get(reason, "readback_mismatch")
                if code not in errors:
                    errors.append(code)
                checks[-1]["error_code"] = code
    complete = REQUIRED_CAPABILITIES.issubset({item["capability"] for item in checks})
    supported = (
        probe_receipt.get("state") == SUPPORTED
        and complete
        and bool(checks)
        and all(item["status"] == "passed" and item["effective_readback"] for item in checks)
    )
    if not complete and "missing_callable" not in errors:
        errors.append("missing_callable")
    state = SUPPORTED if supported else UNSUPPORTED_API
    seed = {
        "generated_at": generated,
        "probe": dict(probe_receipt),
        "config_revision_sha256": config_revision_sha256,
        "disposable_workspace_hmac": disposable_workspace_hmac,
    }
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "receipt_id": hashlib.sha256(_canonical_json(seed).encode("utf-8")).hexdigest(),
        "provider": "honcho",
        "sdk_version": str(probe_receipt.get("sdk_version") or "unknown"),
        "sdk_source_sha256": _sha256_text(probe_receipt.get("sdk_source_hash")),
        "provider_source_sha256": _sha256_text(probe_receipt.get("provider_source_hash")),
        "probe_manifest_sha256": _sha256_text(probe_receipt.get("probe_manifest_hash")),
        "config_revision": _sha256_text(config_revision_sha256),
        "disposable_workspace_hmac": _sha256_text(disposable_workspace_hmac),
        "state": state,
        "checks": checks,
        "paid_chat_probe": (
            "approved_minimal_passed" if paid_seen and paid_approved
            else "approved_minimal_failed" if paid_seen
            else "skipped_unapproved"
        ),
        "observed_at": generated,
        "content_free": True,
    }
    return receipt


def validate_public_capability_receipt(
    receipt: Any,
    *,
    expected_config_revision_sha256: str = "",
) -> bool:
    """Strictly validate the CloudSeed H2 receipt without importing jsonschema."""
    if not isinstance(receipt, Mapping):
        return False
    required = {
        "schema_version", "receipt_id", "provider", "sdk_version",
        "sdk_source_sha256", "provider_source_sha256", "probe_manifest_sha256",
        "config_revision", "disposable_workspace_hmac", "state", "checks",
        "paid_chat_probe", "observed_at", "content_free",
    }
    if set(receipt) != required:
        return False
    if receipt.get("schema_version") != RECEIPT_SCHEMA or receipt.get("provider") != "honcho":
        return False
    if receipt.get("content_free") is not True:
        return False
    for key in (
        "receipt_id", "sdk_source_sha256", "provider_source_sha256",
        "probe_manifest_sha256", "config_revision", "disposable_workspace_hmac",
    ):
        value = receipt.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            return False
    if expected_config_revision_sha256 and receipt.get("config_revision") != expected_config_revision_sha256:
        return False
    checks = receipt.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    seen: set[str] = set()
    for check in checks:
        if not isinstance(check, Mapping):
            return False
        if not {"capability", "status", "effective_readback"}.issubset(check):
            return False
        if not set(check).issubset({"capability", "status", "effective_readback", "error_code"}):
            return False
        name = check.get("capability")
        if not isinstance(name, str) or not name or name in seen:
            return False
        seen.add(name)
        if check.get("status") not in {"passed", "failed"}:
            return False
        if type(check.get("effective_readback")) is not bool:
            return False
    if receipt.get("state") == SUPPORTED:
        if not REQUIRED_CAPABILITIES.issubset(seen):
            return False
        if any(c.get("status") != "passed" or c.get("effective_readback") is not True for c in checks):
            return False

    elif receipt.get("state") != UNSUPPORTED_API:
        return False
    if receipt.get("paid_chat_probe") not in {
        "skipped_unapproved", "approved_minimal_passed", "approved_minimal_failed"
    }:
        return False
    return True


def load_public_capability_receipt(
    path: str | os.PathLike[str],
    *,
    expected_file_sha256: str,
    expected_config_revision_sha256: str = "",
    max_bytes: int = 131072,
) -> dict[str, Any] | None:
    """Read one exact no-follow receipt file and verify its bound bytes and shape."""
    receipt_path = Path(path)
    if not receipt_path.is_absolute() or receipt_path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(receipt_path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(payload) > max_bytes:
        return None
    if hashlib.sha256(payload).hexdigest() != str(expected_file_sha256 or ""):
        return None
    try:
        receipt = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not validate_public_capability_receipt(
        receipt,
        expected_config_revision_sha256=expected_config_revision_sha256,
    ):
        return None
    return dict(receipt)


__all__ = [
    "CapabilityCheck",
    "CapabilityProbeDependencies",
    "HonchoCapabilityProbeDependencies",
    "RECEIPT_SCHEMA",
    "REQUIRED_CAPABILITIES",
    "SUPPORTED",
    "UNSUPPORTED_API",
    "build_public_capability_receipt",
    "load_public_capability_receipt",
    "manifest_hash",
    "probe_capabilities",
    "probe_honcho_capabilities",
    "probe_required_honcho_capabilities",
    "run_capability_probe",
    "validate_public_capability_receipt",
]
